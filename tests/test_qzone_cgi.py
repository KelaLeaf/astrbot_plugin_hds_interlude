# -*- coding: utf-8 -*-
"""`plugin/core/qzone_cgi.py` 的单元测试（QQ 空间 CGI 协议层）。

参考实现是 `Eganchiyu/qzone-sdk`（MIT，零依赖）。本文件的分组逐条对应它的
`src/qzone_sdk/` 模块，断言与它**逐字对比**：

| 参考实现 | 本文件 |
| --- | --- |
| `utils/gtk.py` | `ComputeGTkTests` |
| `auth/base.py::auth_from_cookies`（含 p_skey 锚定坑） | `ExtractCookieTests` / `QZoneAuthTests` |
| `constants.py` | `ConstantsTests` |
| `client.py` 的七个 `_post` / `_get` 调用 | `RequestBuilderTests` |
| `utils/jsonp.py` | `ParseJsonpTests` |
| `utils/feed.py` | `FeedParsingTests` / `MoodParsingTests` |
| `utils/response.py` | `SuccessOrErrorTests` |
| ——（本移植版的纪律） | `ModuleDisciplineTests` |

**字段集合与顺序**这一组是硬断言（`list(body.keys())` 与参考实现的字面量元组相等）：
腾讯的 CGI 靠字段名认参，键顺序也是 wire 的一部分，别顺手"整理"成字母序。

独立运行（仓库根目录；发布仓布局去掉 `plugin.` 前缀）：
    python3 -m unittest plugin.tests.test_qzone_cgi -v
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

from plugin.core.qzone_cgi import (
    QZONE_APP_TYPES,
    QZONE_UA,
    QZONE_URLS,
    QZONE_ALLOW_UINS_SEPARATOR,
    QZONE_UPLOAD_RECEIPT_FIELDS,
    QZONE_VISIBLE,
    QZONE_VISIBLE_TARGETED,
    QZoneAuth,
    QZoneAuthError,
    build_comment_request,
    build_delete_request,
    build_feed_request,
    build_forward_request,
    build_image_richval,
    build_like_request,
    build_mood_list_request,
    build_publish_request,
    build_update_visibility_request,
    build_upload_image_request,
    compute_g_tk,
    extract_cookie,
    feed_items_from_text,
    hex_decode,
    image_upload_receipt,
    parse_feed_item,
    parse_jsonp,
    parse_mood,
    parse_moods_response,
    qzone_auth_from_cookies,
    success_or_error,
)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGIN_ROOT.parent
PACKAGE_NAME = PLUGIN_ROOT.name
MODULE_PATH = PLUGIN_ROOT / 'core' / 'qzone_cgi.py'

#: 「skey 不得被 p_skey 误匹配」的现场：p_skey 在前、skey 根本不存在。
COOKIES_FULL = (
    "RK=abc; uin=o0123456789; skey=@AbCdEfGhIj; p_skey=AbCdEfGhIjKlMnOpQrSt; "
    "qq_locale=zh_CN; pt2gguin=o0123456789"
)
UIN = "123456789"
P_SKEY = "AbCdEfGhIjKlMnOpQrSt"
SKEY = "@AbCdEfGhIj"


def _auth() -> QZoneAuth:
    return qzone_auth_from_cookies(COOKIES_FULL, UIN)


def _auth_raw() -> QZoneAuth:
    """手工造的凭据（不经过 Cookie 串），给只需要 headers 的用例用。"""
    return QZoneAuth(cookies=COOKIES_FULL, g_tk=compute_g_tk(P_SKEY), uin=UIN,
                     skey=SKEY, p_skey=P_SKEY)


# ── g_tk ───────────────────────────────────────────────────────────────


def _g_tk_stepwise(skey: str) -> int:
    """**独立口径**：每步都截断在 32 位无符号里的 djb2 变体（C / PHP 的写法）。

    参考实现把截断放在收尾（`& 0x7FFFFFFF`）；两条路只有在"取模与 `h*33+c` 可交换"
    这个性质成立时才相等——所以这是对算法的真检查，不是抄一遍。
    """
    h = 5381
    for ch in skey:
        h = (h + (h << 5) + ord(ch)) & 0xFFFFFFFF
    return h & 0x7FFFFFFF


def _g_tk_js(skey: str) -> int:
    """**第二条独立口径**：模拟 JS 的 int32 语义（`<<` 按 int32 截断，收尾 `&`）。

    腾讯的原始实现是 JS，这条复刻它每一步的有符号 32 位回绕。
    """

    def i32(x: int) -> int:
        x &= 0xFFFFFFFF
        return x - 0x100000000 if x >= 0x80000000 else x

    h = 5381
    for ch in skey:
        h = h + i32(i32(h) << 5) + ord(ch)
    return i32(h) & 0x7FFFFFFF


def _g_tk_unmasked(skey: str) -> int:
    """漏掉收尾截断时的"错误结果"（Python 无限精度）。"""
    h = 5381
    for ch in skey:
        h += (h << 5) + ord(ch)
    return h


class ComputeGTkTests(unittest.TestCase):
    """`utils/gtk.py::compute_g_tk`。"""

    #: 钉死的已知值（用 JS/PHP 的 32 位路线算出，另见下面两条独立口径对照）。
    PINNED = {
        "": 5381,
        "@abcdefghij": 1148810428,
        "abc": 193485963,
        "p_skey_demo_1234567890": 1930373760,
        P_SKEY: 1200358103,
    }

    def test_pinned_values(self):
        for skey, expected in self.PINNED.items():
            self.assertEqual(compute_g_tk(skey), expected, skey)

    def test_the_pinned_literal_is_the_sdk_route(self):
        """字面量 1148810428 是 JS/PHP/参考实现三条路都同意的那个值。"""
        self.assertEqual(compute_g_tk("@abcdefghij"), 1148810428)
        self.assertEqual(_g_tk_stepwise("@abcdefghij"), 1148810428)
        self.assertEqual(_g_tk_js("@abcdefghij"), 1148810428)

    def test_matches_stepwise_and_js_semantics(self):
        samples = [
            "",
            "a",
            "abc",
            "@abcdefghij",
            P_SKEY,
            "x" * 40,
            "0123456789" * 8,
            "~!@#$%^&*()_+-=[]{}|;:,.<>?/",
            "中文不算边界情况但这串不是 ASCII 也要有确定行为",
        ]
        for skey in samples:
            with self.subTest(skey=skey[:16]):
                expected = _g_tk_stepwise(skey)
                self.assertEqual(compute_g_tk(skey), expected)
                self.assertEqual(_g_tk_js(skey), expected)

    def test_truncation_is_mandatory(self):
        """漏掉 `& 0x7FFFFFFF` 会返回天文数字——这就是那个坑本身。"""
        skey = "x" * 40
        raw = _g_tk_unmasked(skey)
        self.assertGreater(raw, 0x7FFFFFFF)
        self.assertGreater(raw.bit_length(), 64, "无限精度膨胀：确实远不止 32 位")
        self.assertNotEqual(raw, compute_g_tk(skey))
        self.assertEqual(raw & 0x7FFFFFFF, compute_g_tk(skey))

    def test_result_is_a_non_negative_int31(self):
        for skey in ("", "a", P_SKEY, "z" * 64):
            value = compute_g_tk(skey)
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 0x7FFFFFFF)

    def test_empty_skey_is_the_initial_value(self):
        """初始值 5381 是 djb2 的口径，别写成 0（`compute_ptqrtoken` 才是 0）。"""
        self.assertEqual(compute_g_tk(""), 5381)


# ── Cookie ─────────────────────────────────────────────────────────────


class ExtractCookieTests(unittest.TestCase):
    """`extract_cookie`：锚定 Cookie 名起始位置（参考实现专门注释的坑）。"""

    def test_skey_is_not_matched_from_p_skey(self):
        """核心坑：不锚定的话找 `skey` 会命中 `p_skey=` 的子串。"""
        self.assertIsNone(extract_cookie("p_skey=zzz", "skey"))
        self.assertIsNone(extract_cookie("a=1; p_skey=zzz", "skey"))
        # 反向证明：**不锚定**的正则确实会命中，所以锚定不是装饰。
        self.assertIsNotNone(re.search(r"skey=([^;]+)", "p_skey=zzz"))

    def test_skey_prefers_its_own_pair(self):
        self.assertEqual(extract_cookie("skey=aaa; p_skey=bbb", "skey"), "aaa")
        self.assertEqual(extract_cookie("skey=aaa; p_skey=bbb", "p_skey"), "bbb")
        self.assertEqual(extract_cookie("p_skey=bbb; skey=aaa", "skey"), "aaa")

    def test_anchors_on_start_and_after_semicolon(self):
        self.assertEqual(extract_cookie("p_skey=bbb", "p_skey"), "bbb")        # 串首
        self.assertEqual(extract_cookie("a=1; p_skey=bbb", "p_skey"), "bbb")   # 分号+空格
        self.assertEqual(extract_cookie("a=1;p_skey=bbb", "p_skey"), "bbb")    # 分号
        self.assertEqual(extract_cookie("a=1; p_skey=bbb; c=2", "p_skey"), "bbb")
        self.assertEqual(extract_cookie("a=1; p_skey=bbb;", "p_skey"), "bbb")

    def test_does_not_match_a_longer_cookie_name(self):
        """`x_p_skey` 不是 `p_skey`（左侧必须是串首或 `;`）。"""
        self.assertEqual(extract_cookie("x_p_skey=bad; p_skey=good", "p_skey"), "good")
        self.assertIsNone(extract_cookie("x_p_skey=bad", "p_skey"))

    def test_value_stops_at_semicolon_and_keeps_inner_equals(self):
        self.assertEqual(extract_cookie("p_skey=a=b; c=1", "p_skey"), "a=b")
        self.assertEqual(extract_cookie("p_skey=a%3Db; c=1", "p_skey"), "a%3Db")

    def test_missing_or_bad_input_returns_none(self):
        for cookies, name in (
            ("", "p_skey"),
            ("a=1; b=2", "p_skey"),
            ("p_skey=", "p_skey"),          # 空值：`[^;]+` 至少要一个字符
            (None, "p_skey"),
            (12345, "p_skey"),
            (COOKIES_FULL, "nope"),
        ):
            with self.subTest(cookies=cookies, name=name):
                self.assertIsNone(extract_cookie(cookies, name))

    def test_full_cookie_string(self):
        self.assertEqual(extract_cookie(COOKIES_FULL, "p_skey"), P_SKEY)
        self.assertEqual(extract_cookie(COOKIES_FULL, "skey"), SKEY)
        self.assertEqual(extract_cookie(COOKIES_FULL, "uin"), "o0123456789")


class QZoneAuthTests(unittest.TestCase):
    """`qzone_auth_from_cookies`：缺 `p_skey` 明确报错，不静默继续。"""

    def test_missing_p_skey_raises(self):
        with self.assertRaises(QZoneAuthError):
            qzone_auth_from_cookies("uin=o1; skey=@abc; RK=1", UIN)

    def test_skey_alone_does_not_satisfy_p_skey(self):
        """有 `skey` 没有 `p_skey` 也必须报错——锚定坑的下游后果。"""
        with self.assertRaises(QZoneAuthError):
            qzone_auth_from_cookies("skey=@AbCdEfGhIj", UIN)

    def test_auth_error_is_a_value_error(self):
        """调用方即便不认识这个类型，也能按普通取参错误兜住。"""
        self.assertTrue(issubclass(QZoneAuthError, ValueError))
        with self.assertRaises(ValueError):
            qzone_auth_from_cookies("nope=1", UIN)

    def test_bad_input_never_leaks_attribute_error(self):
        for cookies in (None, "", 12345, "a=1"):
            with self.subTest(cookies=cookies):
                with self.assertRaises(QZoneAuthError):
                    qzone_auth_from_cookies(cookies, UIN)

    def test_full_auth(self):
        auth = qzone_auth_from_cookies(COOKIES_FULL, UIN)
        self.assertEqual(auth.cookies, COOKIES_FULL)
        self.assertEqual(auth.uin, UIN)
        self.assertEqual(auth.p_skey, P_SKEY)
        self.assertEqual(auth.skey, SKEY)
        self.assertEqual(auth.g_tk, compute_g_tk(P_SKEY))
        self.assertEqual(auth.g_tk, 1200358103)

    def test_g_tk_comes_from_p_skey_not_skey(self):
        auth = qzone_auth_from_cookies(COOKIES_FULL, UIN)
        self.assertNotEqual(auth.g_tk, compute_g_tk(SKEY))

    def test_skey_is_optional(self):
        """本批七条接口里只有图片上传用 skey，缺了不该拦。"""
        auth = qzone_auth_from_cookies("p_skey=only", UIN)
        self.assertEqual(auth.skey, "")
        self.assertEqual(auth.p_skey, "only")
        self.assertEqual(auth.g_tk, compute_g_tk("only"))

    def test_uin_is_coerced_to_str(self):
        auth = qzone_auth_from_cookies(COOKIES_FULL, 123456789)
        self.assertEqual(auth.uin, "123456789")
        self.assertIsInstance(auth.uin, str)

    def test_auth_is_frozen(self):
        auth = _auth()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            auth.g_tk = 1  # type: ignore[misc]


# ── 常量 ───────────────────────────────────────────────────────────────


class ConstantsTests(unittest.TestCase):
    """`constants.py` 的逐字照抄。"""

    def test_ua_is_the_chrome_ua(self):
        self.assertIn("Chrome/120.0.0.0", QZONE_UA)
        self.assertTrue(QZONE_UA.startswith("Mozilla/5.0 (Windows NT 10.0; Win64; x64)"))

    def test_urls_are_the_nine_cgi_endpoints(self):
        self.assertEqual(
            sorted(QZONE_URLS),
            ["comment", "delete", "feed", "forward", "like", "mood_list",
             "publish", "update", "upload_image"],
        )
        prefix = "https://user.qzone.qq.com/proxy/domain/"
        for name, url in QZONE_URLS.items():
            with self.subTest(name=name):
                if name == "upload_image":
                    # 上传**不在** `/proxy/domain` 下面（参考实现 `api_zone.py` 的
                    # `self.upload_url` 就是 `up.qzone.qq.com`）——别顺手"统一"掉。
                    self.assertEqual(
                        url, "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image",
                    )
                    continue
                self.assertTrue(url.startswith(prefix), url)
        self.assertEqual(
            QZONE_URLS["publish"],
            prefix + "taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6",
        )
        self.assertEqual(
            QZONE_URLS["delete"],
            prefix + "taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6",
        )
        self.assertEqual(
            QZONE_URLS["comment"],
            prefix + "taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds",
        )
        self.assertEqual(
            QZONE_URLS["forward"],
            prefix + "taotao.qzone.qq.com/cgi-bin/emotion_cgi_forward_v6",
        )
        self.assertEqual(
            QZONE_URLS["like"],
            prefix + "w.qzone.qq.com/cgi-bin/likes/internal_dolike_app",
        )
        self.assertEqual(
            QZONE_URLS["feed"],
            prefix + "ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more",
        )
        self.assertEqual(
            QZONE_URLS["mood_list"],
            prefix + "taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
        )

    def test_app_types(self):
        self.assertEqual(QZONE_APP_TYPES, {
            "311": "说说",
            "202": "日志",
            "1": "照片",
            "2": "相册",
            "10": "分享",
            "6600": "广告",
        })

    def test_visible_ranges(self):
        """五档可见范围（v1.7.5 修正：原先照抄 qzone-sdk 的 `{1,3,4}` 是错的）。

        权威依据是 NapCat 的 `ValidUgcRights = [1, 4, 16, 64, 128]`
        （`packages/napcat-core/data/qzone.ts`，注释逐字写了每档含义），上游 Koishi 的
        `QZONE_UGC_RIGHT_VALUES` 与 qzone-sdk 仓库内附带的参考实现同表。`3` 从来不是
        合法取值——留着它会让"好友可见"这条配置静默落到一个服务端不认识的值上。
        """
        self.assertEqual(QZONE_VISIBLE, {
            "public": 1, "friends": 4, "partial": 16, "exclude": 128, "private": 64,
        })
        self.assertEqual(QZONE_VISIBLE_TARGETED, (16, 128))
        self.assertEqual(QZONE_ALLOW_UINS_SEPARATOR, "|")


# ── 请求构造 ───────────────────────────────────────────────────────────

#: 字段集合与顺序：逐字抄自参考实现的 dict 字面量，**顺序**也是断言的一部分。
PUBLISH_KEYS = (
    "syn_tweet_verson", "paramstr", "pic_template", "richtype", "richval",
    "special_url", "subrichtype", "pic_bo", "who", "con", "feedversion", "ver",
    "ugc_right", "to_sign", "hostuin", "code_version", "format", "qzreferrer",
)
#: `emotion_cgi_update` 的字段与顺序：逐字抄自参考实现
#: `qzone_api/api/api_parms.py::build_edit_message_params` 的 dict 字面量
#: （`allow_uins` 是条件追加的，见下面专门的用例）。
UPDATE_KEYS = (
    "syn_tweet_verson", "tid", "paramstr", "pic_template", "richtype", "richval",
    "special_url", "subrichtype", "con", "feedversion", "ver", "ugc_right",
    "to_sign", "ugcright_id", "hostuin", "code_version", "format", "qzreferrer",
)
DELETE_KEYS = (
    "uin", "topicId", "feedsType", "feedsFlag", "feedsKey", "feedsAppid",
    "feedsTime", "fupdate", "ref", "qzreferrer",
)
LIKE_KEYS = (
    "qzreferrer", "opuin", "unikey", "curkey", "appid", "from", "typeid",
    "abstime", "fid", "active", "format", "fupdate",
)
COMMENT_KEYS = (
    "topicId", "feedsType", "inCharset", "outCharset", "plat", "source",
    "hostUin", "isSignIn", "platformid", "uin", "format", "ref", "content",
    "richval", "richtype", "private", "paramstr", "qzreferrer",
)
FORWARD_KEYS = (
    "t1_uin", "t1_source", "tid", "signin", "con", "with_cmt", "fwdToWeibo",
    "forward_source", "code_version", "format", "hostuin", "qzreferrer",
)
FEED_KEYS = (
    "uin", "scope", "view", "filter", "flag", "applist", "pagenum", "count",
    "format", "g_tk", "useutf8", "outputhtmlfeed",
)
MOOD_LIST_KEYS = (
    "uin", "ftype", "sort", "pos", "num", "replynum", "g_tk", "code_version",
    "format", "need_private_comment", "callback",
)

POST_HEADER_KEYS = ("Cookie", "User-Agent", "Referer", "Content-Type", "Origin")
GET_HEADER_KEYS = ("Cookie", "User-Agent", "Referer")


class RequestBuilderTests(unittest.TestCase):
    """八个 `build_*_request`：`(method, url, headers, body)`。

    body 是**有序 dict**：POST 是表单字段，GET 是查询参数。键顺序逐字对齐参考实现。
    """

    def setUp(self):
        self.auth = _auth()
        self.g_tk = self.auth.g_tk

    # 公共形状 -----------------------------------------------------------

    def _assert_common_headers(self, headers, expect_post):
        self.assertEqual(
            tuple(headers), POST_HEADER_KEYS if expect_post else GET_HEADER_KEYS
        )
        self.assertEqual(headers["Cookie"], COOKIES_FULL)
        self.assertEqual(headers["User-Agent"], QZONE_UA)
        if expect_post:
            self.assertEqual(
                headers["Content-Type"], "application/x-www-form-urlencoded"
            )
            self.assertEqual(headers["Origin"], "https://user.qzone.qq.com")

    def test_post_urls_carry_g_tk_and_get_urls_do_not(self):
        posts = [
            build_publish_request(self.auth, "hi"),
            build_update_visibility_request(self.auth, "tid", "正文", 4),
            build_delete_request(self.auth, "tid"),
            build_like_request(self.auth, "1", "fid", "ck", "uk"),
            build_comment_request(self.auth, "1", "fid", "c"),
            build_forward_request(self.auth, "1", "tid"),
        ]
        for method, url, _, _ in posts:
            with self.subTest(url=url):
                self.assertEqual(method, "POST")
                self.assertTrue(url.endswith(f"?g_tk={self.g_tk}"), url)
        for url, expected in (
            (build_feed_request(self.auth)[1], QZONE_URLS["feed"]),
            (build_mood_list_request(self.auth, "1")[1], QZONE_URLS["mood_list"]),
        ):
            with self.subTest(url=url):
                self.assertEqual(url, expected)
                self.assertNotIn("g_tk=", url, "读操作的 g_tk 在 body 里")

    # 改可见范围（v1.7.5 新增：`emotion_cgi_update`） ---------------------

    def test_update_visibility_body_matches_the_reference_field_list(self):
        """字段与**顺序**逐字来自参考实现的 `build_edit_message_params`。"""
        method, url, headers, body = build_update_visibility_request(
            self.auth, "tid-1", "今天天气不错", 4,
        )
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{QZONE_URLS['update']}?g_tk={self.g_tk}")
        self.assertIn("emotion_cgi_update", url)
        self.assertTrue(url.endswith(f"?g_tk={self.g_tk}"), "写操作的 g_tk 挂 URL")
        self._assert_common_headers(headers, expect_post=True)
        self.assertEqual(tuple(body), UPDATE_KEYS)
        self.assertEqual(body["tid"], "tid-1")
        self.assertEqual(body["con"], "今天天气不错")
        self.assertEqual(body["ugc_right"], "4")
        self.assertEqual(body["hostuin"], self.auth.uin)
        self.assertEqual(body["ugcright_id"], "", "参考实现缺省就是空串")
        self.assertEqual(body["format"], "fs")
        # 参考实现的编辑构造器**没有** `who`（发布有），照它，不加。
        self.assertNotIn("who", body)

    def test_update_visibility_target_uins_are_pipe_joined(self):
        """`allow_uins` 用 `|` 分隔（NapCat 的 `data/qzone.ts` + 它自己的单测钉的）。"""
        for right in (16, 128):
            with self.subTest(right=right):
                body = build_update_visibility_request(
                    self.auth, "t", "正文", right, ["10001", 10002, " 10003 "],
                )[3]
                self.assertEqual(body["ugc_right"], str(right))
                self.assertEqual(body["allow_uins"], "10001|10002|10003")

    def test_update_visibility_drops_the_uins_for_the_other_three_tiers(self):
        """1 / 4 / 64 三档不带 `allow_uins`（带了只会让请求更容易被判非法）。"""
        for right in (1, 4, 64):
            with self.subTest(right=right):
                body = build_update_visibility_request(
                    self.auth, "t", "正文", right, ["10001"],
                )[3]
                self.assertNotIn("allow_uins", body)
        # 16/128 但名单是空的 / 不是列表：同样不带（服务端看不懂空名单）。
        for empty in ((), [], None, "", "10001"):
            with self.subTest(empty=empty):
                body = build_update_visibility_request(
                    self.auth, "t", "正文", 16, empty,
                )[3]
                self.assertNotIn("allow_uins", body)

    def test_update_visibility_refuses_an_empty_body_text(self):
        """空正文直接抛：这条接口按整条重建，空 `con` 等于把正文清掉。"""
        for content in ("", "   ", None):
            with self.subTest(content=content):
                with self.assertRaises(QZoneAuthError):
                    build_update_visibility_request(self.auth, "t", content, 4)

    def test_every_builder_returns_a_four_tuple_of_the_right_types(self):
        requests = (
            build_publish_request(self.auth, "hi"),
            build_update_visibility_request(self.auth, "tid", "正文", 4),
            build_delete_request(self.auth, "tid"),
            build_like_request(self.auth, "1", "fid", "ck", "uk"),
            build_comment_request(self.auth, "1", "fid", "c"),
            build_forward_request(self.auth, "1", "tid"),
            build_feed_request(self.auth),
            build_mood_list_request(self.auth, "1"),
        )
        for request in requests:
            with self.subTest(method=request[0]):
                self.assertEqual(len(request), 4)
                method, url, headers, body = request
                self.assertIn(method, ("GET", "POST"))
                self.assertTrue(url.startswith("https://"))
                self.assertIsInstance(headers, dict)
                self.assertIsInstance(body, dict)
                self.assertTrue(all(isinstance(v, str) for v in body.values()),
                                "表单值一律字符串（腾讯不认 JSON 数字）")
                self.assertTrue(all(isinstance(v, str) for v in headers.values()))

    def test_referer_and_qzreferrer_use_the_target_homepage(self):
        """点赞 / 评论的 `qzreferrer` 指着**对方**的主页，Referer 仍是自己的。"""
        _, _, headers, body = build_like_request(
            self.auth, "98765", "fid", "ck", "uk"
        )
        self.assertEqual(headers["Referer"], f"https://user.qzone.qq.com/{UIN}")
        self.assertEqual(body["qzreferrer"], "https://user.qzone.qq.com/98765")
        _, _, _, body = build_comment_request(self.auth, "98765", "fid", "c")
        self.assertEqual(body["qzreferrer"], "https://user.qzone.qq.com/98765")

    # 各接口 -------------------------------------------------------------

    def test_publish(self):
        method, url, headers, body = build_publish_request(self.auth, "今天很好")
        self.assertEqual(url, f"{QZONE_URLS['publish']}?g_tk={self.g_tk}")
        self._assert_common_headers(headers, expect_post=True)
        self.assertEqual(tuple(body), PUBLISH_KEYS)
        self.assertEqual(body, {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "pic_template": "",
            "richtype": "",
            "richval": "",
            "special_url": "",
            "subrichtype": "",
            "pic_bo": "",
            "who": "1",
            "con": "今天很好",
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": UIN,
            "code_version": "1",
            "format": "fs",
            "qzreferrer": f"https://user.qzone.qq.com/{UIN}",
        })

    def test_publish_visible_is_stringified(self):
        for visible, expected in ((1, "1"), (3, "3"), (4, "4")):
            with self.subTest(visible=visible):
                _, _, _, body = build_publish_request(self.auth, "x", visible=visible)
                self.assertEqual(body["ugc_right"], expected)
                self.assertIsInstance(body["ugc_right"], str)

    def test_publish_richval_flips_richtype_like_the_sdk_image_branch(self):
        _, _, _, body = build_publish_request(
            self.auth, "带图", richval="l1,l1,l1", pic_bo="bo1"
        )
        self.assertEqual(body["richval"], "l1,l1,l1")
        self.assertEqual(body["richtype"], "1")
        self.assertEqual(body["subrichtype"], "1")
        self.assertEqual(body["pic_bo"], "bo1")
        # 空 richval 时仍是纯文本分支的取值（坑：别把 richtype 恒置 "1"）。
        _, _, _, plain = build_publish_request(self.auth, "无图")
        self.assertEqual(plain["richtype"], "")
        self.assertEqual(plain["subrichtype"], "")
        self.assertEqual(plain["pic_bo"], "")

    def test_delete(self):
        method, url, headers, body = build_delete_request(self.auth, "TID1")
        self.assertEqual(url, f"{QZONE_URLS['delete']}?g_tk={self.g_tk}")
        self._assert_common_headers(headers, expect_post=True)
        self.assertEqual(tuple(body), DELETE_KEYS)
        self.assertEqual(body, {
            "uin": UIN,
            "topicId": "TID1",
            "feedsType": "0",
            "feedsFlag": "0",
            "feedsKey": "TID1",
            "feedsAppid": "311",
            "feedsTime": "0",
            "fupdate": "1",
            "ref": "feeds",
            "qzreferrer": f"https://user.qzone.qq.com/{UIN}",
        })

    def test_delete_curkey_falls_back_to_tid(self):
        _, _, _, body = build_delete_request(self.auth, "TID1", curkey="CK1")
        self.assertEqual(body["feedsKey"], "CK1")
        _, _, _, body = build_delete_request(self.auth, "TID1", timestamp=1700000000)
        self.assertEqual(body["feedsKey"], "TID1")
        self.assertEqual(body["feedsTime"], "1700000000")
        # 空 curkey 与显式空串都走 tid 兜底（`curkey or tid`）。
        _, _, _, body = build_delete_request(self.auth, "TID1", curkey="")
        self.assertEqual(body["feedsKey"], "TID1")

    def test_like(self):
        method, url, headers, body = build_like_request(
            self.auth, "98765", "FID1", "CK1", "UK1", abstime=1700000000
        )
        self.assertEqual(url, f"{QZONE_URLS['like']}?g_tk={self.g_tk}")
        self._assert_common_headers(headers, expect_post=True)
        self.assertEqual(tuple(body), LIKE_KEYS)
        self.assertEqual(body, {
            "qzreferrer": "https://user.qzone.qq.com/98765",
            "opuin": UIN,
            "unikey": "UK1",
            "curkey": "CK1",
            "appid": "311",
            "from": "1",
            "typeid": "0",
            "abstime": "1700000000",
            "fid": "FID1",
            "active": "0",
            "format": "json",
            "fupdate": "1",
        })

    def test_like_abstime_defaults_to_now(self):
        before = int(time.time())
        _, _, _, body = build_like_request(self.auth, "9", "f", "c", "u")
        after = int(time.time())
        self.assertLessEqual(before, int(body["abstime"]))
        self.assertLessEqual(int(body["abstime"]), after)

    def test_like_abstime_zero_is_not_replaced_by_now(self):
        """`0` 是合法入参（显式给了就别当"没给"）。"""
        _, _, _, body = build_like_request(self.auth, "9", "f", "c", "u", abstime=0)
        self.assertEqual(body["abstime"], "0")

    def test_comment(self):
        method, url, headers, body = build_comment_request(
            self.auth, "98765", "123456789_TID1__1", "写得好"
        )
        self.assertEqual(url, f"{QZONE_URLS['comment']}?g_tk={self.g_tk}")
        self._assert_common_headers(headers, expect_post=True)
        self.assertEqual(tuple(body), COMMENT_KEYS)
        self.assertEqual(body, {
            "topicId": "123456789_TID1__1",
            "feedsType": "100",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "hostUin": "98765",
            "isSignIn": "",
            "platformid": "52",
            "uin": UIN,
            "format": "fs",
            "ref": "feeds",
            "content": "写得好",
            "richval": "",
            "richtype": "",
            "private": "0",
            "paramstr": "1",
            "qzreferrer": "https://user.qzone.qq.com/98765",
        })

    def test_forward(self):
        method, url, headers, body = build_forward_request(
            self.auth, "98765", "TID1", "转发一下"
        )
        self.assertEqual(url, f"{QZONE_URLS['forward']}?g_tk={self.g_tk}")
        self._assert_common_headers(headers, expect_post=True)
        self.assertEqual(tuple(body), FORWARD_KEYS)
        self.assertEqual(body, {
            "t1_uin": "98765",
            "t1_source": "1",
            "tid": "TID1",
            "signin": "0",
            "con": "转发一下",
            "with_cmt": "0",
            "fwdToWeibo": "0",
            "forward_source": "2",
            "code_version": "1",
            "format": "fs",
            "hostuin": UIN,
            "qzreferrer": f"https://user.qzone.qq.com/{UIN}",
        })

    def test_forward_content_defaults_to_empty(self):
        _, _, _, body = build_forward_request(self.auth, "98765", "TID1")
        self.assertEqual(body["con"], "")
        self.assertEqual(tuple(body), FORWARD_KEYS)

    def test_feed(self):
        method, url, headers, body = build_feed_request(self.auth, page=2, count=5)
        self.assertEqual(method, "GET")
        self.assertEqual(url, QZONE_URLS["feed"])
        self._assert_common_headers(headers, expect_post=False)
        self.assertEqual(tuple(body), FEED_KEYS)
        self.assertEqual(body, {
            "uin": UIN,
            "scope": "0",
            "view": "1",
            "filter": "all",
            "flag": "1",
            "applist": "all",
            "pagenum": "2",
            "count": "5",
            "format": "json",
            "g_tk": str(self.g_tk),
            "useutf8": "1",
            "outputhtmlfeed": "1",
        })

    def test_feed_defaults(self):
        _, _, _, body = build_feed_request(self.auth)
        self.assertEqual(body["pagenum"], "1")
        self.assertEqual(body["count"], "10")

    def test_mood_list(self):
        method, url, headers, body = build_mood_list_request(self.auth, "98765", num=20)
        self.assertEqual(method, "GET")
        self.assertEqual(url, QZONE_URLS["mood_list"])
        self._assert_common_headers(headers, expect_post=False)
        self.assertEqual(tuple(body), MOOD_LIST_KEYS)
        self.assertEqual(body, {
            "uin": "98765",
            "ftype": "0",
            "sort": "0",
            "pos": "0",
            "num": "20",
            "replynum": "100",
            "g_tk": str(self.g_tk),
            "code_version": "1",
            "format": "jsonp",
            "need_private_comment": "1",
            "callback": "_preloadCallback",
        })

    def test_mood_list_callback_is_what_the_jsonp_parser_expects(self):
        """请求点名的 callback 必须就是 `parse_jsonp` 认的第一条口径。"""
        _, _, _, body = build_mood_list_request(self.auth, "9")
        self.assertEqual(body["callback"], "_preloadCallback")
        self.assertEqual(body["format"], "jsonp")
        payload = body["callback"] + '({"code":0,"msglist":[]});'
        self.assertEqual(parse_jsonp(payload), {"code": 0, "msglist": []})

    def test_builders_accept_an_auth_built_by_hand(self):
        """调用方可能自己补 uin / cookies（不走 Cookie 串解析）。"""
        auth = _auth_raw()
        _, url, headers, _ = build_delete_request(auth, "TID")
        self.assertTrue(url.endswith(f"?g_tk={compute_g_tk(P_SKEY)}"))
        self.assertEqual(headers["Cookie"], COOKIES_FULL)

    def test_builders_accept_a_mapping_auth(self):
        """`core/qzone.py::qzone_cgi_request` 的 `auth` 标注是 `Mapping`——dict 也要能用。

        两种形态必须产出**逐字相同**的请求，否则接入方会在"用对象还是用 dict"上
        得到两套 wire。
        """
        mapping = {
            "cookies": COOKIES_FULL,
            "g_tk": compute_g_tk(P_SKEY),
            "uin": UIN,
            "skey": SKEY,
            "p_skey": P_SKEY,
        }
        auth = _auth_raw()
        cases = [
            (build_publish_request, ("hi",), {}),
            (build_publish_request, ("hi",),
             {"visible": 3, "richval": "r", "pic_bo": "b"}),
            (build_delete_request, ("TID",), {}),
            (build_delete_request, ("TID",),
             {"curkey": "CK", "timestamp": 1700000000}),
            (build_like_request, ("9", "FID", "CK", "UK"), {"abstime": 1700000000}),
            (build_comment_request, ("9", "FID", "内容"), {}),
            (build_forward_request, ("9", "TID"), {"content": "附言"}),
            (build_feed_request, (), {}),
            (build_feed_request, (), {"page": 3, "count": 25}),
            (build_mood_list_request, ("9",), {}),
            (build_mood_list_request, ("9",), {"num": 30}),
        ]
        for builder, args, kwargs in cases:
            with self.subTest(builder=builder.__name__, args=args, kwargs=kwargs):
                from_mapping = builder(mapping, *args, **kwargs)
                from_object = builder(auth, *args, **kwargs)
                self.assertEqual(from_mapping[0], from_object[0])
                self.assertEqual(from_mapping[1], from_object[1])
                self.assertEqual(list(from_mapping[2].items()),
                                 list(from_object[2].items()))
                self.assertEqual(list(from_mapping[3].items()),
                                 list(from_object[3].items()))

    def test_empty_mapping_auth_yields_empty_credentials_not_a_crash(self):
        """缺字段不抛 KeyError/AttributeError：拼出来是空凭据，由调用方去判可用性。"""
        for builder, args in (
            (build_publish_request, ("hi",)),
            (build_delete_request, ("TID",)),
            (build_like_request, ("9", "F", "C", "U")),
            (build_comment_request, ("9", "F", "c")),
            (build_forward_request, ("9", "T")),
            (build_feed_request, ()),
            (build_mood_list_request, ("9",)),
        ):
            with self.subTest(builder=builder.__name__):
                method, url, headers, body = builder({}, *args)
                self.assertEqual(headers["Cookie"], "")
                self.assertEqual(headers["Referer"], "https://user.qzone.qq.com/")
                if method == "POST":
                    self.assertIn("g_tk=", url)
                else:
                    self.assertEqual(body["g_tk"], "")


# ── 图片上传 + richval（v1.7.8）────────────────────────────────────────


#: `cgi_upload_image` 的字段与顺序：逐字抄自参考实现
#: `qzone_api/api/api_parms.py::build_upload_image_params` 的 dict 字面量。
UPLOAD_KEYS = (
    "filename", "zzpanelkey", "uploadtype", "albumtype", "exttype", "skey",
    "zzpaneluin", "p_uin", "uin", "p_skey", "output_type", "qzonetoken",
    "refer", "charset", "output_charset", "upload_hd", "hd_width", "hd_height",
    "hd_quality", "backUrls", "url", "base64", "picfile",
)

#: 一份编的上传回执（`data` 里的六个字段就是 `build_image_richval` 的全部输入）。
RECEIPT_ONE = {
    "albumid": "ALB-1", "lloc": "LLOC-1", "sloc": "SLOC-1", "type": 1,
    "height": 480, "width": 640, "url": "https://example.invalid/p?bo=BO-1&x=1",
}
RECEIPT_TWO = {
    "albumid": "ALB-2", "lloc": "LLOC-2", "sloc": "SLOC-2", "type": 1,
    "height": 100, "width": 200, "url": "https://example.invalid/p?bo=BO-2&x=1",
}


class ImageUploadTests(unittest.TestCase):
    """`cgi_upload_image` 的请求构造 + 上传回执 → `richval` / `pic_bo`。

    依据：参考实现 `qzone_api/api/api_parms.py` 的 `build_upload_image_params`
    与 `build_image_richval`（逐字对照）。**上传成功是带图改可见范围的前提**，
    所以"拼不出来就回空 dict"这条必须是硬断言。
    """

    def setUp(self):
        self.auth = _auth()

    def test_upload_body_matches_the_reference_field_list(self):
        method, url, headers, body = build_upload_image_request(self.auth, "QUJD")
        self.assertEqual(method, "POST")
        self.assertEqual(
            url,
            f"{QZONE_URLS['upload_image']}?g_tk={self.auth.g_tk}",
        )
        self.assertTrue(url.startswith("https://up.qzone.qq.com/cgi-bin/upload/"))
        self.assertEqual(tuple(body), UPLOAD_KEYS, "字段与顺序逐字对齐参考实现")
        self.assertEqual(body["skey"], SKEY, "skey 直接从 Cookie 带进表单")
        self.assertEqual(body["p_skey"], P_SKEY)
        self.assertEqual(body["uin"], UIN)
        self.assertEqual(body["zzpaneluin"], UIN)
        self.assertEqual(body["p_uin"], UIN)
        self.assertEqual(body["albumtype"], "7", "说说相册")
        self.assertEqual(body["refer"], "shuoshuo")
        self.assertEqual(body["output_type"], "json")
        self.assertEqual(body["base64"], "1")
        self.assertEqual(body["picfile"], "QUJD", "`picfile` 就是调用方给的 base64")
        self.assertEqual(body["upload_hd"], "1")
        self.assertEqual(body["hd_width"], "2048")
        self.assertEqual(body["hd_height"], "10000")
        self.assertEqual(body["hd_quality"], "96")
        self.assertEqual(body["url"], url, "表单里的 url 与请求地址是同一个")
        self.assertIn("upbak.photo.qzone.qq.com", body["backUrls"], "备用上传点在参考实现里")
        self.assertTrue(all(isinstance(value, str) for value in body.values()))
        self.assertEqual(tuple(headers), POST_HEADER_KEYS)

    def test_upload_refuses_without_skey_or_p_skey(self):
        """缺 skey / p_skey 本地就报错：它们空着必然被服务端拒，别让上层拿到含糊的 code。"""
        base = QZoneAuth(cookies=COOKIES_FULL, g_tk=self.auth.g_tk, uin=UIN,
                         skey=SKEY, p_skey=P_SKEY)
        self.assertEqual(build_upload_image_request(base, "QUJD")[3]["skey"], SKEY)
        for broken in (
            dataclasses.replace(base, skey=""),
            dataclasses.replace(base, p_skey=""),
            dataclasses.replace(base, skey="", p_skey=""),
        ):
            with self.subTest(skey=broken.skey, p_skey=broken.p_skey):
                with self.assertRaises(QZoneAuthError):
                    build_upload_image_request(broken, "QUJD")

    def test_image_upload_receipt_reads_the_data_field(self):
        payload = {"code": 0, "data": dict(RECEIPT_ONE), "msg": "success"}
        self.assertEqual(image_upload_receipt(payload), RECEIPT_ONE)
        for broken in (None, {}, {"code": 0}, {"code": 0, "data": "nope"}, "text", 7):
            with self.subTest(broken=broken):
                self.assertEqual(image_upload_receipt(broken), {})

    def test_richval_is_the_reference_literal(self):
        """单图：`",{albumid},{lloc},{sloc},{type},{height},{width},,{height},{width}"`。"""
        rich = build_image_richval([RECEIPT_ONE])
        self.assertEqual(rich["richval"], ",ALB-1,LLOC-1,SLOC-1,1,480,640,,480,640")
        self.assertEqual(rich["pic_bo"], "BO-1", "`bo` 从回执 url 里抠出来")
        self.assertEqual(set(rich), {"richval", "pic_bo"})

    def test_multi_image_richval_joins_with_tabs(self):
        """组图：每张一段、段间 `\\t`（不是逗号——两种分隔符都在参考实现里，别"统一"）。"""
        rich = build_image_richval([RECEIPT_ONE, RECEIPT_TWO])
        self.assertEqual(
            rich["richval"],
            ",ALB-1,LLOC-1,SLOC-1,1,480,640,,480,640"
            "\t,ALB-2,LLOC-2,SLOC-2,1,100,200,,100,200",
        )
        self.assertEqual(rich["pic_bo"], "BO-1\tBO-2")
        self.assertEqual(rich["richval"].count(","), 18, "两张图各 9 个逗号")

    def test_a_receipt_missing_any_field_yields_nothing(self):
        """六个字段缺任何一个 → **空 dict**（调用方据此拒绝，不许拼半个 richval）。"""
        self.assertEqual(
            QZONE_UPLOAD_RECEIPT_FIELDS,
            ("albumid", "lloc", "sloc", "type", "height", "width"),
        )
        for field in QZONE_UPLOAD_RECEIPT_FIELDS:
            with self.subTest(field=field):
                broken = dict(RECEIPT_ONE)
                broken.pop(field)
                self.assertEqual(build_image_richval([broken]), {})
                broken[field] = ""
                self.assertEqual(build_image_richval([broken]), {})

    def test_richval_refuses_empty_or_non_mapping_input(self):
        for broken in (None, [], (), [None], ["x"], [{"albumid": "a"}], 0, "x"):
            with self.subTest(broken=broken):
                self.assertEqual(build_image_richval(broken), {})

    def test_a_missing_bo_only_loses_the_pic_bo(self):
        """回执 url 里没有 `bo=`：`richval` 照拼，`pic_bo` 为空串（参考实现同口径）。"""
        receipt = dict(RECEIPT_ONE, url="https://example.invalid/p?x=1")
        self.assertEqual(
            build_image_richval([receipt]),
            {"richval": ",ALB-1,LLOC-1,SLOC-1,1,480,640,,480,640", "pic_bo": ""},
        )


class RichTextUpdateTests(unittest.TestCase):
    """改可见范围的请求**永远不带富文本块**（v1.7.9 的 H1 裁定）。

    参考实现的 `build_edit_message_params` 就是"改可见范围"用的构造器，它的
    `pic_template` / `richtype` / `richval` / `subrichtype` / `special_url` 全是空串
    ——"空串 = 不改动富文本"。所以这里钉两件事：① 这些槽位**在**字段清单里且**恒为空**；
    ② 构造器**根本不给**调用方塞 `richval` / `pic_bo` 的口子（旧版本那条口子会把
    "重新上传原图"接回来）。见 `docs/PORTING_NOTES.md` §43。
    """

    def setUp(self):
        self.auth = _auth()

    def test_text_path_is_byte_identical_to_the_reference_list(self):
        """字段集合与顺序**逐字**是参考实现那一份（回归）。"""
        body = build_update_visibility_request(self.auth, "tid-1", "正文", 4)[3]
        self.assertEqual(tuple(body), UPDATE_KEYS)
        self.assertNotIn("pic_bo", body)

    def test_the_update_never_fills_a_rich_text_slot(self):
        """五个富文本槽位恒为空串——这是"不改动图片 / 视频"的全部依据。"""
        body = build_update_visibility_request(self.auth, "tid-1", "正文", 4)[3]
        self.assertEqual(body["pic_template"], "")
        self.assertEqual(body["richtype"], "")
        self.assertEqual(body["subrichtype"], "")
        self.assertEqual(body["richval"], "")
        self.assertEqual(body["special_url"], "")

    def test_the_builder_has_no_rich_text_parameters(self):
        """构造器**不接收** `richval` / `pic_bo`：想在编辑路径上塞富文本只能改这个签名。"""
        parameters = inspect.signature(build_update_visibility_request).parameters
        self.assertEqual(
            tuple(parameters),
            ("auth", "tid", "content", "visible", "target_uins", "ugcright_id"),
        )

    def test_the_targeted_tiers_keep_allow_uins_and_still_no_rich_text(self):
        body = build_update_visibility_request(
            self.auth, "tid-1", "正文", 16, ["10001", "10002"],
        )[3]
        self.assertEqual(body["allow_uins"], "10001|10002")
        self.assertEqual(body["richval"], "")
        self.assertEqual(body["richtype"], "")

    def test_an_empty_content_still_raises(self):
        for content in ("", "   ", None):
            with self.subTest(content=content):
                with self.assertRaises(QZoneAuthError):
                    build_update_visibility_request(self.auth, "tid-1", content, 4)


# ── JSONP ──────────────────────────────────────────────────────────────


class ParseJsonpTests(unittest.TestCase):
    """`utils/jsonp.py`：坏输入返回 `None`，不抛到上层。"""

    def test_preload_callback(self):
        self.assertEqual(
            parse_jsonp('_preloadCallback({"code":0,"msglist":[]});'),
            {"code": 0, "msglist": []},
        )

    def test_preload_callback_without_semicolon(self):
        self.assertEqual(parse_jsonp('_preloadCallback({"code":0})'), {"code": 0})

    def test_other_callback_shapes(self):
        self.assertEqual(
            parse_jsonp('frameElement.callback({"code":0})'), {"code": 0}
        )
        self.assertEqual(parse_jsonp('_Callback({"code":1})'), {"code": 1})
        self.assertEqual(parse_jsonp('callback({"code":2})'), {"code": 2})

    def test_bare_json_falls_back(self):
        self.assertEqual(parse_jsonp('  {"code": 0}  '), {"code": 0})
        self.assertEqual(parse_jsonp('{"a": {"b": 1}}'), {"a": {"b": 1}})

    def test_bad_input_returns_none(self):
        for raw in (
            None,
            "",
            "   ",
            "<html>502 Bad Gateway</html>",
            "_preloadCallback({bad json});",
            "{not json",
            "[1, 2, 3]",
            12345,
            b'{"code":0}',
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_jsonp(raw))

    def test_never_raises_on_garbage(self):
        """随意截断的 JSONP 也不能把异常扔给动作层。"""
        payload = '_preloadCallback({"code":0,"msglist":[{"tid":"t"}]});'
        for cut in range(len(payload)):
            with self.subTest(cut=cut):
                parse_jsonp(payload[:cut])  # 不抛即可


# ── feed 解析 ──────────────────────────────────────────────────────────

#: 一条动态的原始段（`{ver:` 之后的文本）。`html:` 里的转义与参考实现面对的一致：
#: `\xNN` 按字节转义、`\"` 与 `\/` 各转义一层 —— 所以这里用**原始字符串**写，
#: 让转义真的留在待解析的文本里（普通字符串会把 `\"` 在 Python 层就吃掉，
#: 那样"反转义"这条路径就永远测不到）。
FEED_ITEM_DATA_ORIGINURL = (
    r"1,key:'FeedKeyA',appid:311,typeid:0,opuin:123456789,uin:123456789,"
    r"nickname:'小明',remark:'备注名',abstime:'1700000000',feedstime:'今天 12:00',"
    r"scope:'1',cmtnum:'2',likecount:'5',"
    r"html:'\x3cdiv class=\"f-info f-info-first\"\x3e今天天气不错\x3c/div\x3e"
    r"\x3cdiv class=\"f-info\"\x3e第二行\x3c/div\x3e"
    r"\x3cimg src=\"https:\/\/qlogo2.store.qq.com\/avatar.jpg\" \/\x3e"
    r"\x3cimg data-originurl=\"https:\/\/photo.store.qq.com\/psb?\/abc\" \/\x3e'}"
)

FEED_ITEM_SRC_ONLY = (
    r"1,key:'FeedKeyB',appid:1,nickname:'小红',feedstime:'昨天',"
    r"html:'\x3cdiv class=\"f-info\"\x3e只有图片\x3c/div\x3e"
    r"\x3cimg src=\"https:\/\/qlogo1.store.qq.com\/avatar.jpg\" \/\x3e"
    r"\x3cimg src=\"https:\/\/r.photo.store.qq.com\/psb?\/xyz\" \/\x3e'}"
)

FEED_ITEM_NO_IMAGES = (
    r"1,key:'FeedKeyC',appid:999,abstime:1700000001,"
    r"html:'\x3cdiv class=\"f-info\"\x3e没有图\x3c/div\x3e'}"
)


class FeedParsingTests(unittest.TestCase):
    """`utils/feed.py`：抠元数据 + `\\xNN` 解码 + 图片过滤。"""

    def test_feed_items_from_text_splits_on_ver_marker(self):
        page = "<html>前缀</html>{ver:" + FEED_ITEM_DATA_ORIGINURL + "{ver:" + FEED_ITEM_SRC_ONLY
        items = feed_items_from_text(page)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["key"], "FeedKeyA")
        self.assertEqual(items[1]["key"], "FeedKeyB")

    def test_feed_items_from_text_tolerates_bad_input(self):
        for raw in (None, "", "no marker here", 12345, "{ver:", "{ver:   "):
            with self.subTest(raw=raw):
                self.assertEqual(feed_items_from_text(raw), [])

    def test_parse_feed_item_metadata(self):
        feed = parse_feed_item(FEED_ITEM_DATA_ORIGINURL)
        self.assertEqual(tuple(feed), (
            "key", "appid", "appid_name", "typeid", "opuin", "uin", "nickname",
            "remark", "abstime", "feedstime", "scope", "cmtnum", "likecount",
            "content", "images",
        ))
        self.assertEqual(feed["key"], "FeedKeyA")
        self.assertEqual(feed["appid"], "311")
        self.assertEqual(feed["appid_name"], "说说")
        self.assertEqual(feed["typeid"], "0")
        self.assertEqual(feed["opuin"], "123456789")
        self.assertEqual(feed["uin"], "123456789")
        self.assertEqual(feed["nickname"], "小明")
        self.assertEqual(feed["remark"], "备注名")
        self.assertEqual(feed["abstime"], "1700000000")
        self.assertEqual(feed["feedstime"], "今天 12:00")
        self.assertEqual(feed["scope"], "1")
        self.assertEqual(feed["cmtnum"], 2)
        self.assertEqual(feed["likecount"], 5)

    def test_parse_feed_item_appid_name_mapping(self):
        for appid, expected in (
            ("311", "说说"), ("202", "日志"), ("1", "照片"), ("2", "相册"),
            ("10", "分享"), ("6600", "广告"), ("999", "未知(999)"),
        ):
            with self.subTest(appid=appid):
                raw = f"1,key:'k',appid:{appid},html:''}}"
                self.assertEqual(parse_feed_item(raw)["appid_name"], expected)

    def test_parse_feed_item_unquoted_numbers(self):
        """`abstime` / `cmtnum` / `likecount` 有引号无引号两种形态。"""
        feed = parse_feed_item(FEED_ITEM_NO_IMAGES)
        self.assertEqual(feed["abstime"], "1700000001")
        self.assertEqual(feed["cmtnum"], 0)
        self.assertEqual(feed["likecount"], 0)
        quoted = parse_feed_item(
            "1,key:'k',abstime:'1700000002',cmtnum:7,likecount:9,html:''}"
        )
        self.assertEqual(quoted["abstime"], "1700000002")
        self.assertEqual(quoted["cmtnum"], 7)
        self.assertEqual(quoted["likecount"], 9)

    def test_hex_decode(self):
        self.assertEqual(hex_decode(r"\x3cdiv\x3e"), "<div>")
        self.assertEqual(hex_decode(r"\x4A\x6b"), "Jk")
        self.assertEqual(hex_decode("no escapes"), "no escapes")
        # 只认两位十六进制；`\xZZ` 与半截转义原样留着。
        self.assertEqual(hex_decode(r"\xZZ"), r"\xZZ")
        self.assertEqual(hex_decode(r"\x4"), r"\x4")

    def test_content_is_decoded_and_tags_stripped(self):
        feed = parse_feed_item(FEED_ITEM_DATA_ORIGINURL)
        self.assertEqual(feed["content"], "今天天气不错\n第二行")

    def test_content_unescapes_quotes_and_slashes(self):
        """解码顺序：先 `\\xNN`，再 `\\"` 与 `\\/`。反了 URL 就带反斜杠。"""
        raw = (
            r"1,key:'k',appid:311,html:'\x3cdiv class=\"f-info\"\x3e看"
            r"\x3ca href=\"https:\/\/user.qzone.qq.com\/123\"\x3e这里\x3c/a\x3e"
            r"\x3c/div\x3e'}"
        )
        feed = parse_feed_item(raw)
        self.assertEqual(feed["content"], "看这里")

    def test_images_prefer_data_originurl(self):
        feed = parse_feed_item(FEED_ITEM_DATA_ORIGINURL)
        self.assertEqual(feed["images"], ["https://photo.store.qq.com/psb?/abc"])
        self.assertNotIn("qlogo", "".join(feed["images"]))

    def test_images_fall_back_to_src_and_drop_qlogo(self):
        """没有 `data-originurl` 时回退 `src=`，但头像（`qlogo`）必须排掉。"""
        feed = parse_feed_item(FEED_ITEM_SRC_ONLY)
        self.assertEqual(feed["images"], ["https://r.photo.store.qq.com/psb?/xyz"])

    def test_src_fallback_only_accepts_photo_pic_qlogo_hosts(self):
        """照抄参考实现的回退正则：`src=` 里不含 photo/pic/qlogo 的一律不收。"""
        raw = (
            r"1,key:'k',appid:311,html:'\x3cdiv class=\"f-info\"\x3ex\x3c/div\x3e"
            r"\x3cimg src=\"https:\/\/example.com\/a.png\" \/\x3e'}"
        )
        self.assertEqual(parse_feed_item(raw)["images"], [])

    def test_no_images_and_no_content(self):
        feed = parse_feed_item(FEED_ITEM_NO_IMAGES)
        self.assertEqual(feed["content"], "没有图")
        self.assertEqual(feed["images"], [])

    def test_empty_html_gives_empty_content(self):
        feed = parse_feed_item("1,key:'k',appid:311,html:''}")
        self.assertEqual(feed["content"], "")
        self.assertEqual(feed["images"], [])

    def test_parse_feed_item_tolerates_bad_input(self):
        for raw in (None, "", 12345, {"key": "x"}):
            with self.subTest(raw=raw):
                feed = parse_feed_item(raw)
                self.assertEqual(tuple(feed), (
                    "key", "appid", "appid_name", "typeid", "opuin", "uin",
                    "nickname", "remark", "abstime", "feedstime", "scope",
                    "cmtnum", "likecount", "content", "images",
                ))
                self.assertEqual(feed["content"], "")
                self.assertEqual(feed["images"], [])
                self.assertEqual(feed["cmtnum"], 0)
                self.assertEqual(feed["likecount"], 0)
                self.assertEqual(feed["appid_name"], "未知()")


# ── 说说解析 ───────────────────────────────────────────────────────────


MOOD_RAW = {
    "tid": "TID1",
    "content": "今天很好",
    "created_time": 1700000000,
    "createTime": "2023-11-15 06:13:20",
    "cmtnum": 3,
    "fwdnum": 1,
    "name": "小明",
    "uin": 123456789,
    "lbs": {"name": "北京市", "pos_x": "1.0"},
    "pic": [
        {"url1": "https://photo.store.qq.com/a.jpg",
         "smallurl": "https://photo.store.qq.com/a_s.jpg",
         "width": 800, "height": 600},
    ],
    "rt_tid": "TID0",
    "rt_uin": 987654321,
    "rt_con": {"content": "被转发的原文"},
}


class MoodParsingTests(unittest.TestCase):
    """`utils/feed.py::parse_mood` + `parse_moods_response`。"""

    def test_parse_mood_fields(self):
        mood = parse_mood(MOOD_RAW)
        self.assertEqual(tuple(mood), (
            "tid", "content", "created_time", "createTime", "cmtnum", "fwdnum",
            "likecount", "name", "uin", "lbs", "pic", "video", "rt_tid", "rt_uin",
            "rt_content",
        ))
        self.assertEqual(mood["tid"], "TID1")
        self.assertEqual(mood["content"], "今天很好")
        self.assertEqual(mood["created_time"], 1700000000)
        self.assertEqual(mood["createTime"], "2023-11-15 06:13:20")
        self.assertEqual(mood["cmtnum"], 3)
        self.assertEqual(mood["fwdnum"], 1)
        self.assertIsNone(mood["likecount"], '这份回执没说点赞数 → None（不是 0）')
        self.assertEqual(mood["name"], "小明")
        self.assertEqual(mood["uin"], 123456789)
        self.assertEqual(mood["lbs"], {"name": "北京市", "pos_x": "1.0"})
        self.assertEqual(mood["video"], [], "没有 video 字段时是空列表（不是缺失）")

    def test_parse_mood_keeps_the_like_count_when_the_receipt_has_one(self):
        """rc33「被点赞感知」：回执里**本来就有**的赞数要留下来（多种拼写都认）。

        参考实现只取 `cmtnum` / `fwdnum`，腾讯在说说列表里的赞数字段名没有保证，
        所以三种常见拼写都收；一个都没有才留 `None`（"这条回执没说"）。
        `None` 与 `0` 在下游增量比对里语义相反，绝不能混。
        """
        for key in ("likecount", "like_num", "likenum"):
            with self.subTest(key=key):
                mood = parse_mood({"tid": "t", key: "7"})
                self.assertEqual(mood["likecount"], 7, key)
        self.assertIsNone(parse_mood({"tid": "t", "likecount": None})["likecount"])
        self.assertIsNone(parse_mood({"tid": "t", "likecount": "坏值"})["likecount"])
        self.assertEqual(parse_mood({"tid": "t", "likecount": 0})["likecount"], 0,
                         '确实没人赞 = 0，与"回执没说"（None）不是一回事')

    def test_parse_mood_video_maps_the_reference_fields(self):
        """`video` 六个字段逐字照抄参考实现 `html_parser.py::parse_feed_data`。

        加这个字段是为了**改可见范围后的回读校验**能数出视频（视频不在正文里，
        只看 `pic` 会把"视频被删"漏成"什么都没变"）。
        """
        mood = parse_mood({"tid": "t", "video": [
            {"url3": "https://video.invalid/v.mp4", "url1": "https://video.invalid/c.jpg",
             "video_id": "VID-1", "video_time": "12345",
             "cover_width": 640, "cover_height": 480},
            "坏元素",
        ]})
        self.assertEqual(mood["video"], [{
            "url": "https://video.invalid/v.mp4",
            "cover": "https://video.invalid/c.jpg",
            "video_id": "VID-1",
            "duration_ms": "12345",
            "width": 640,
            "height": 480,
        }])

    def test_parse_mood_video_tolerates_bad_shapes(self):
        for raw in (None, "字符串", 7, {"a": 1}):
            with self.subTest(raw=raw):
                self.assertEqual(parse_mood({"tid": "t", "video": raw})["video"], [])

    def test_parse_mood_pic_maps_url1(self):
        mood = parse_mood(MOOD_RAW)
        self.assertEqual(mood["pic"], [{
            "url": "https://photo.store.qq.com/a.jpg",
            "smallurl": "https://photo.store.qq.com/a_s.jpg",
            "width": 800,
            "height": 600,
        }])

    def test_parse_mood_forward_fields(self):
        mood = parse_mood(MOOD_RAW)
        self.assertEqual(mood["rt_tid"], "TID0")
        self.assertEqual(mood["rt_uin"], 987654321)
        self.assertEqual(mood["rt_content"], "被转发的原文")

    def test_parse_mood_without_forward(self):
        mood = parse_mood({"tid": "t", "content": "c"})
        self.assertEqual(mood["rt_tid"], "")
        self.assertEqual(mood["rt_uin"], "")
        self.assertEqual(mood["rt_content"], "")

    def test_parse_mood_tolerates_bad_input(self):
        for raw in (None, "字符串", 12345, []):
            with self.subTest(raw=raw):
                mood = parse_mood(raw)
                self.assertEqual(mood["tid"], "")
                self.assertEqual(mood["pic"], [])
                self.assertEqual(mood["lbs"], {})
                self.assertEqual(mood["rt_content"], "")

    def test_parse_mood_tolerates_bad_nested_shapes(self):
        """`pic` 是 None / 混了非 dict、`rt_con` 是字符串——都不许抛。"""
        mood = parse_mood({
            "tid": "t",
            "pic": None,
            "rt_tid": "x",
            "rt_uin": 1,
            "rt_con": "不是 dict",
        })
        self.assertEqual(mood["pic"], [])
        self.assertEqual(mood["rt_content"], "")

        mood = parse_mood({"pic": ["坏元素", {"url1": "u"}]})
        self.assertEqual(mood["pic"], [{
            "url": "u", "smallurl": "", "width": 0, "height": 0,
        }])

    def test_parse_mood_pic_missing_fields(self):
        mood = parse_mood({"pic": [{}]})
        self.assertEqual(mood["pic"], [{
            "url": "", "smallurl": "", "width": 0, "height": 0,
        }])


class ParseMoodsResponseTests(unittest.TestCase):
    """JSONP → `{'code': 0, 'moods': [...]}`；非 0 一律空列表。"""

    @staticmethod
    def _jsonp(payload: dict) -> str:
        return "_preloadCallback(" + json.dumps(payload, ensure_ascii=False) + ");"

    def test_success(self):
        result = parse_moods_response(self._jsonp({"code": 0, "msglist": [MOOD_RAW]}))
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["moods"]), 1)
        self.assertEqual(result["moods"][0]["tid"], "TID1")
        self.assertEqual(result["moods"][0]["content"], "今天很好")

    def test_empty_msglist(self):
        self.assertEqual(
            parse_moods_response(self._jsonp({"code": 0, "msglist": []})),
            {"code": 0, "moods": []},
        )

    def test_non_zero_code_is_not_success(self):
        """未登录 / 风控也是 `code != 0`——绝不能当成"有动态"。"""
        result = parse_moods_response(
            self._jsonp({"code": -10000, "subcode": 0, "message": "未登录"})
        )
        self.assertEqual(result["code"], -10000)
        self.assertEqual(result["moods"], [])

    def test_missing_code_is_a_failure(self):
        """参考实现是 `data.get("code") != 0`：缺 code 也算失败。"""
        result = parse_moods_response(self._jsonp({"msglist": [MOOD_RAW]}))
        self.assertEqual(result["code"], -1)
        self.assertEqual(result["moods"], [])

    def test_bad_input_returns_empty(self):
        for text in (None, "", "   ", "<html>围墙</html>", "_preloadCallback(oops);", 12345):
            with self.subTest(text=text):
                self.assertEqual(parse_moods_response(text), {"code": -1, "moods": []})

    def test_msglist_not_a_list(self):
        result = parse_moods_response(self._jsonp({"code": 0, "msglist": {"a": 1}}))
        self.assertEqual(result, {"code": 0, "moods": []})

    def test_bad_mood_row_degrades_to_empty_item(self):
        result = parse_moods_response(self._jsonp({"code": 0, "msglist": ["坏行"]}))
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["moods"]), 1)
        self.assertEqual(result["moods"][0]["tid"], "")

    def test_frame_element_callback_shape(self):
        text = "frameElement.callback(" + json.dumps({"code": 0, "msglist": [MOOD_RAW]}) + ")"
        self.assertEqual(len(parse_moods_response(text)["moods"]), 1)

    def test_never_raises_on_truncated_payload(self):
        payload = self._jsonp({"code": 0, "msglist": [MOOD_RAW]})
        for cut in range(len(payload)):
            with self.subTest(cut=cut):
                parse_moods_response(payload[:cut])


# ── 写操作结果 ─────────────────────────────────────────────────────────


class SuccessOrErrorTests(unittest.TestCase):
    """`utils/response.py`：五条写操作共用这一条判定口径。"""

    def test_parse_failure_branch(self):
        expected = {"success": False, "code": -1, "message": "响应解析失败"}
        for result in (None, {}, "", 0, "raw text", b"raw", []):
            with self.subTest(result=result):
                self.assertEqual(success_or_error(result), expected)

    def test_success_branch(self):
        result = success_or_error({
            "code": 0,
            "t1_tid": "T1",
            "t1_time": "1700000000",
            "content": "今天很好",
            "t1_curkey": "CK1",
        })
        self.assertEqual(result, {
            "success": True,
            "tid": "T1",
            "time": "1700000000",
            "content": "今天很好",
            "curkey": "CK1",
        })

    def test_success_uses_tid_when_t1_tid_is_absent(self):
        result = success_or_error({"code": 0, "tid": "T2"})
        self.assertEqual(result["success"], True)
        self.assertEqual(result["tid"], "T2")
        self.assertEqual(result["time"], "")
        self.assertEqual(result["content"], "")
        self.assertNotIn("curkey", result)

    def test_success_curkey_prefers_curkey_then_t1_curkey(self):
        self.assertEqual(success_or_error({"code": 0, "curkey": "A"})["curkey"], "A")
        self.assertEqual(success_or_error({"code": 0, "t1_curkey": "B"})["curkey"], "B")
        self.assertEqual(
            success_or_error({"code": 0, "curkey": "A", "t1_curkey": "B"})["curkey"], "A"
        )
        # 空串 curkey 不进结果（免得上层拿它去删说说）。
        self.assertNotIn("curkey", success_or_error({"code": 0, "curkey": ""}))

    def test_success_accepts_ret_zero(self):
        """少数接口回 `ret` 而不是 `code`。"""
        result = success_or_error({"ret": 0, "tid": "T3"})
        self.assertEqual(result["success"], True)
        self.assertEqual(result["tid"], "T3")

    def test_failure_branch(self):
        result = success_or_error({"code": -3000, "message": "操作太频繁"})
        self.assertEqual(result, {
            "success": False, "code": -3000, "message": "操作太频繁",
        })

    def test_failure_falls_back_to_msg(self):
        self.assertEqual(
            success_or_error({"code": -1, "msg": "bad"})["message"], "bad"
        )
        # `message` 优先于 `msg`（两者都有时取 message）。
        self.assertEqual(
            success_or_error({"code": -1, "message": "M", "msg": "m"})["message"], "M"
        )

    def test_failure_without_message_says_unknown(self):
        result = success_or_error({"code": -1})
        self.assertEqual(result, {"success": False, "code": -1, "message": "未知错误"})
        # 空字符串 message 也走兜底（`or` 语义）。
        self.assertEqual(
            success_or_error({"code": -1, "message": ""})["message"], "未知错误"
        )

    def test_failure_reads_ret_code(self):
        self.assertEqual(success_or_error({"ret": -2})["code"], -2)

    def test_code_key_present_but_none(self):
        """照抄参考实现的取值顺序：有 `code` 键就不再回落到 `ret`。"""
        result = success_or_error({"code": None, "ret": 0})
        self.assertEqual(result["success"], False)
        self.assertIsNone(result["code"])

    def test_no_code_no_ret(self):
        result = success_or_error({"foo": 1})
        self.assertEqual(result["success"], False)
        self.assertEqual(result["code"], -1)
        self.assertEqual(result["message"], "未知错误")

    def test_one_verdict_for_all_write_ops(self):
        """发 / 删 / 赞 / 评 / 转 共用同一口径——别给某个动作开小灶。"""
        ok = {"code": 0, "tid": "T"}
        bad = {"code": -1, "message": "风控"}
        builders = (
            lambda: success_or_error(ok),
            lambda: success_or_error(dict(ok)),
            lambda: success_or_error(dict(ok)),
            lambda: success_or_error(dict(ok)),
            lambda: success_or_error(dict(ok)),
        )
        for build in builders:
            self.assertTrue(build()["success"])
        self.assertFalse(success_or_error(bad)["success"])


# ── 模块纪律 ───────────────────────────────────────────────────────────

FORBIDDEN_IMPORTS = (
    "astrbot", "httpx", "requests", "aiohttp", "urllib", "urllib3", "socket",
    "http", "asyncio", "ssl", "os", "io", "pathlib", "subprocess", "xmlrpc",
    "smtplib", "ftplib", "telnetlib", "webbrowser",
)


class ModuleDisciplineTests(unittest.TestCase):
    """`core/` 的既有纪律：不 import astrbot、不自己发网络请求。"""

    def test_source_imports_nothing_forbidden(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {name.split(".")[0] for name in imported}
        offenders = sorted(roots & set(FORBIDDEN_IMPORTS))
        self.assertEqual(offenders, [], f"协议层出现了不该有的依赖：{offenders}")
        self.assertIn("re", roots)
        self.assertIn("json", roots)

    def test_clean_process_import_does_not_leak_astrbot(self):
        program = (
            "import sys, importlib\n"
            f"importlib.import_module({PACKAGE_NAME + '.core.qzone_cgi'!r})\n"
            "leaked = sorted(m for m in sys.modules\n"
            "                if m == 'astrbot' or m.startswith('astrbot.'))\n"
            "heavy = sorted(m for m in sys.modules\n"
            "               if m.split('.')[0] in ('httpx', 'requests', 'aiohttp'))\n"
            "print('LEAKED:' + ','.join(leaked))\n"
            "print('HEAVY:' + ','.join(heavy))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", program],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = dict(
            line.split(":", 1) for line in proc.stdout.splitlines() if ":" in line
        )
        self.assertEqual(lines.get("LEAKED"), "", proc.stdout)
        self.assertEqual(lines.get("HEAVY"), "", proc.stdout)

    def test_no_io_call_sites(self):
        """协议层只拼不请求：AST 里不该出现任何 I/O 调用点。

        `dict.get` / `re.search` 这类同名方法不算——只看**顶层名字**上的调用
        （`open(...)` / `urlopen(...)` / `session.post(...)` 的 `session` 是裸名字）。
        """
        io_attrs = {"post", "request", "urlopen", "send", "connect", "read", "write"}
        io_names = {"open", "print", "input", "eval", "exec", "__import__"}
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in io_names:
                offenders.append(f"{func.id}()")
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in io_attrs
                and isinstance(func.value, ast.Name)
            ):
                offenders.append(f"{func.value.id}.{func.attr}()")
        self.assertEqual(offenders, [], f"协议层出现了 I/O 调用点：{offenders}")

    def test_module_defines_no_class_other_than_the_auth_holders(self):
        """别在这里长出"客户端"来——客户端属于注入网络的调用方。"""
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        classes = sorted(
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        )
        self.assertEqual(classes, ["QZoneAuth", "QZoneAuthError"])


if __name__ == "__main__":
    unittest.main()
