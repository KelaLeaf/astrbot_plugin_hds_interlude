"""QQ 空间 CGI 协议层 —— **纯 wire**：只拼请求 / 只解析响应，不发网络请求。

## 为什么有这个模块

AstrBot 通过 WebSocket 连 NapCat，所以"QQ 空间"这条路的正确走法是：
先用 NapCat 的 `get_cookies`（`domain=user.qzone.qq.com`）+ `get_login_info` 拿到
**Cookie 与 uin**，再**直接对腾讯的 QZone CGI 发 HTTP**（Cookie 带上，`g_tk` 由
`p_skey` 算出）。这样点赞 / 评论 / 看动态 / 看好友说说只装 NapCat 也能用。

本模块只负责**协议**：常量、凭据解析、请求构造、响应解析。**HTTP 由调用方注入**
（`httpx` / 宿主的 HTTP 能力都行），因此这里不 import `httpx` / `urllib`，也不
import `astrbot`（`plugin/core/` 的既有纪律）。

## 调用方契约

所有 `build_*_request()` 返回四元组 `(method, url, headers, body)`：

* `method`：`"GET"` / `"POST"`；
* `url`：完整地址。写操作**已在 URL 上带 `?g_tk=<auth.g_tk>`**（照抄参考实现）；
  读操作（feed / mood_list）不带，`g_tk` 在 `body` 里；
* `headers`：`Cookie` / `User-Agent` / `Referer`，POST 另加 `Content-Type` 与 `Origin`；
* `body`：**有序 dict**。`POST` = 表单字段（`application/x-www-form-urlencoded`），
  `GET` = 查询参数。**dict 的键顺序就是参考实现的字段顺序**，别重排。
* `auth`：`QZoneAuth`（`qzone_auth_from_cookies` 的产物）**或**同名字段的 `Mapping`
  （`cookies` / `g_tk` / `uin` / `skey` / `p_skey`）——两种形态产出逐字相同的请求，
  调用方不必为了调用本层先包一个对象。

```python
auth = qzone_auth_from_cookies(cookies, uin)          # 缺 p_skey 直接抛
method, url, headers, body = build_like_request(auth, "123", fid, curkey, unikey)
raw = await http.request(method, url, headers=headers,
                         data=body if method == "POST" else None,
                         params=body if method == "GET" else None)
ok = success_or_error(parse_jsonp(raw) if isinstance(raw, str) else raw)
```

## 与参考实现（Eganchiyu/qzone-sdk，MIT，零依赖）的对应

逐条对照 `client.py` / `constants.py` / `utils/gtk.py` / `utils/feed.py` /
`utils/response.py` / `utils/jsonp.py`。**键名法**：发给外部接口的字段名是 wire 名，
**逐字照抄**（`topicId` / `feedsKey` / `ugc_right` / `curkey` / `unikey` … 大小写与
下划线都不许动）；本模块的 Python 标识符用 snake_case。

**第八条接口 `update`（改已发说说的可见范围，v1.7.5 新增）**不在上面那份 client 里
（它只有发布 / 删除 / 评论 / 点赞 / 转发 / 两条读），来源是同一个仓库内**附带的参考实现**
`._ref/qzone_api-1.1.0/`（= PyPI `qzone-api` 1.1.0，其上游客源站已下线）：
`qzone_api/api/api_zone.py` 的 `update_url` +
`qzone_api/api/api_feed.py::edit_message` + `qzone_api/api/api_parms.py::build_edit_message_params`
（该文件的 docstring 自称"真实抓包确认"，字段清单逐字照抄）。两处**有分歧**的地方按
更权威的一方处理，并在 `docs/PORTING_NOTES.md` 记着：

* `allow_uins` 的分隔符：参考实现说逗号，NapCat 的
  `packages/napcat-core/data/qzone.ts` 说 `|`，且 NapCat 自己的单测
  （`packages/napcat-test/qzone.test.ts`）钉着 `'10001|10002'` ——用 **`|`**；
* 编辑请求要不要 `who`：参考实现的编辑构造器**没有** `who`（发布有），照它，不加。

刻意不做的事：图片上传（`cgi_upload_image` 不在这批交付里）、重试 / 限流 / 审计
（属于调用方的策略层）、任何网络动作。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "QZONE_UA",
    "QZONE_URLS",
    "QZONE_APP_TYPES",
    "QZONE_VISIBLE",
    "QZoneAuthError",
    "QZoneAuth",
    "compute_g_tk",
    "extract_cookie",
    "qzone_auth_from_cookies",
    "build_publish_request",
    "build_update_visibility_request",
    "build_delete_request",
    "build_comment_request",
    "build_forward_request",
    "build_like_request",
    "build_feed_request",
    "build_mood_list_request",
    "hex_decode",
    "parse_jsonp",
    "parse_feed_item",
    "feed_items_from_text",
    "parse_mood",
    "parse_moods_response",
    "success_or_error",
]

# ── A. 常量（逐字照抄 constants.py）────────────────────────────────────

#: 浏览器 UA。腾讯对裸客户端 UA 会返回 403，别换。
QZONE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_QZONE_BASE = "https://user.qzone.qq.com/proxy/domain"

#: QZone CGI 接口地址（前七条逐字照抄 `constants.py` 的 `URLS`；`update` 见模块 docstring）。
QZONE_URLS: Dict[str, str] = {
    "publish": f"{_QZONE_BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6",
    "delete": f"{_QZONE_BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6",
    "comment": f"{_QZONE_BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds",
    "forward": f"{_QZONE_BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_forward_v6",
    "like": f"{_QZONE_BASE}/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app",
    "feed": f"{_QZONE_BASE}/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more",
    "mood_list": f"{_QZONE_BASE}/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
    # 改已发说说的可见范围（参考实现 `qzone_api/api/api_zone.py::update_url`；注意**没有** `_v6`）。
    "update": f"{_QZONE_BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_update",
}

#: `allow_uins`（部分人可见 / 部分人不可见）生效的那两档 `ugc_right`。
QZONE_VISIBLE_TARGETED: tuple[int, ...] = (16, 128)

#: `allow_uins` 里多个 QQ 的分隔符（NapCat `data/qzone.ts` 的
#: "多个QQ号使用 | 拼接"，其单测钉着 `'10001|10002'`）。
QZONE_ALLOW_UINS_SEPARATOR = "|"

#: appid（字符串！接口给的是字符串）→ 动态类型名。
QZONE_APP_TYPES: Dict[str, str] = {
    "311": "说说",
    "202": "日志",
    "1": "照片",
    "2": "相册",
    "10": "分享",
    "6600": "广告",
}

#: 说说可见范围（`ugc_right` 的取值，**五档**）。
#:
#: ⚠️ v1.7.5 **修正**：本表原先照抄 qzone-sdk `constants.py` 的
#: `{"public": 1, "friends": 3, "private": 4}`，其中 **3 不是合法取值**，且
#: `friends=3 / private=4` 与本插件其它地方（`core/qzone.py::qzone_visibility_label`、
#: 发说说默认 `ugc_right=4` 表示"好友可见"）自相矛盾。权威依据：
#:
#: * **NapCat** `packages/napcat-core/data/qzone.ts` 与
#:   `packages/napcat-onebot/action/extends/SendQzoneMsg.ts`：
#:   `ValidUgcRights = [1, 4, 16, 64, 128]`，注释逐字是
#:   "1所有人可见 4好友可见 16部分好友可见 64仅自己可见 128部分好友不可见"；
#: * **上游 Koishi** `QZONE_UGC_RIGHT_VALUES = {1,4,16,64,128}` 与 `qzoneVisibilityLabel`
#:   （本仓库 `core/qzone.py` 的 `qzone_visibility_label` 就是它的移植）；
#: * qzone-sdk 仓库内附带的参考实现 `._ref/qzone_api-1.1.0/qzone_api/api/api_parms.py`
#:   的 `UGC_RIGHT_ALL / FRIENDS / PART / EXCLUDE / PRIVATE` 常量同表。
QZONE_VISIBLE: Dict[str, int] = {
    "public": 1,     # 所有人可见
    "friends": 4,    # 仅 QQ 好友可见
    "partial": 16,   # 部分人可见（`allow_uins` = 白名单）
    "exclude": 128,  # 部分人不可见（`allow_uins` = 黑名单）
    "private": 64,   # 仅自己可见
}

#: `build_*_request` 的返回：`(method, url, headers, body)`。
QZoneRequest = Tuple[str, str, Dict[str, str], Dict[str, str]]


# ── A2. 凭据 ───────────────────────────────────────────────────────────


class QZoneAuthError(ValueError):
    """凭据不完整（典型：Cookie 里没有 `p_skey`）。

    继承 `ValueError`：调用方既不认识这个类型也能按普通取参错误处理。
    """


@dataclass(frozen=True)
class QZoneAuth:
    """一次 QZone 调用需要的全部凭据。

    `g_tk` 由 `p_skey` 算出并**缓存**在这里——别在每次请求时重算（同一个值，
    但重算意味着调用方要再拿一次 `p_skey`）。
    """

    cookies: str
    g_tk: int
    uin: str
    skey: str = ""
    p_skey: str = ""


def compute_g_tk(p_skey: str) -> int:
    """由 `p_skey` 计算 `g_tk`（QQ 空间固定算法，djb2 变体，初始 5381）。

    **坑（必须记）**：Python 的整数是无限精度的，`h` 会随字符串长度指数级膨胀
    （40 字符的 p_skey 就上万亿位），而 JS/PHP 每步都截断在 32 位里。所以最后那句
    `& 0x7FFFFFFF` **不是可选的美化**——漏掉它返回的就是一个天文数字，服务端一律
    当无效 token。

    这里照抄参考实现（只在收尾截断一次）：因为 `h -> h*33 + c` 与取模 2^31 可交换，
    收尾截断与逐步截断**结果相同**（`test_qzone_cgi.ComputeGTkTests` 里有逐步截断
    与 JS int32 语义两条独立口径对照钉住）。若哪天改成非线性的哈希，就必须改成每步
    截断了。
    """
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def extract_cookie(cookies: str, name: str) -> Optional[str]:
    """从 Cookie 串里取指定名字的值；没有返回 `None`。

    **坑（参考实现专门注释过）**：正则**必须锚定** Cookie 名起始位置
    （`(?:^|;\\s*)`）。不锚定的话找 `skey` 会先命中 `p_skey=` 的**子串**
    （`...; p_skey=xxx` 里的 `skey=xxx` 看着完全像合法匹配），于是 `skey` 悄悄变成
    `p_skey` 的值——登录态还没坏，但任何依赖 `skey` 的分支都拿到错数据。
    """
    if not isinstance(cookies, str) or not cookies:
        return None
    m = re.search(r"(?:^|;\s*)" + re.escape(name) + r"=([^;]+)", cookies)
    return m.group(1) if m else None


def qzone_auth_from_cookies(cookies: str, uin: str) -> QZoneAuth:
    """从 NapCat `get_cookies` 拿到的 Cookie 串 + `get_login_info` 的 uin 造凭据。

    缺 `p_skey` **明确报错**（`QZoneAuthError`）：没有它就算不出 `g_tk`，所有写操作
    都会被服务端拒；静默继续只会把"未登录"伪装成"操作失败"，让上层分不清是风控还是
    凭据过期。

    `skey` 可以缺（本批交付的七条接口里只有图片上传用它）。
    """
    p_skey = extract_cookie(cookies, "p_skey")
    if not p_skey:
        raise QZoneAuthError("Cookie 缺少 p_skey，请确认已登录或重新获取凭证")
    return QZoneAuth(
        cookies=cookies if isinstance(cookies, str) else "",
        g_tk=compute_g_tk(p_skey),
        uin=str(uin),
        skey=extract_cookie(cookies, "skey") or "",
        p_skey=p_skey,
    )


# ── B. 请求构造 ────────────────────────────────────────────────────────


def _auth_field(auth: Any, name: str) -> Any:
    """取凭据字段：`QZoneAuth` 走属性，调用方自己拼的 dict 走键。

    调用方（`core/qzone.py`）的 `qzone_cgi_request(action, auth, …)` 声明的就是
    `Mapping`——所以两种形态都收，别让它因为"auth 是 dict"就 AttributeError。
    """
    if isinstance(auth, Mapping):
        return auth.get(name, "")
    return getattr(auth, name, "")


def _headers(auth: Any, post: bool = False) -> Dict[str, str]:
    """公共请求头（照抄 `client.py::_headers`）。"""
    h = {
        "Cookie": _auth_field(auth, "cookies"),
        "User-Agent": QZONE_UA,
        "Referer": f"https://user.qzone.qq.com/{_auth_field(auth, 'uin')}",
    }
    if post:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        h["Origin"] = "https://user.qzone.qq.com"
    return h


def _authed_url(key: str, auth: Any) -> str:
    """写操作地址：把 `g_tk` 挂在 URL 上（读操作走 body）。"""
    return f"{QZONE_URLS[key]}?g_tk={_auth_field(auth, 'g_tk')}"


def _allow_uins(target_uins: Any) -> str:
    """把 QQ 名单拼成 `allow_uins`（`|` 分隔；空 / 非列表一律空串）。"""
    if isinstance(target_uins, (str, bytes)) or not isinstance(target_uins, (list, tuple, set)):
        return ""
    parts = [str(item).strip() for item in target_uins if str(item).strip()]
    return QZONE_ALLOW_UINS_SEPARATOR.join(parts)


def build_publish_request(
    auth: Any,
    content: str,
    visible: int = 1,
    richval: str = "",
    pic_bo: str = "",
) -> QZoneRequest:
    """发说说。`visible`：1 所有人 / 4 好友 / 16 部分可见 / 64 仅自己 / 128 部分不可见（见 `QZONE_VISIBLE`）。

    `richval` / `pic_bo` 是**已上传图片**的产物（图片上传不在这批交付里，由调用方
    自己走 `cgi_upload_image` 后回填）。给了 `richval` 就照参考实现的图片分支把
    `richtype` / `subrichtype` 置 `"1"`——空 `richval` 时字段值与参考实现的纯文本
    分支逐字一致。
    """
    uin = _auth_field(auth, "uin")
    referer = f"https://user.qzone.qq.com/{uin}"
    data: Dict[str, str] = {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "pic_template": "",
        "richtype": "",
        "richval": "",
        "special_url": "",
        "subrichtype": "",
        "pic_bo": "",
        "who": "1",
        "con": content,
        "feedversion": "1",
        "ver": "1",
        "ugc_right": str(visible),
        "to_sign": "0",
        "hostuin": uin,
        "code_version": "1",
        "format": "fs",
        "qzreferrer": referer,
    }
    if richval:
        data["richtype"] = "1"
        data["subrichtype"] = "1"
        data["richval"] = richval
    if pic_bo:
        data["pic_bo"] = pic_bo
    return "POST", _authed_url("publish", auth), _headers(auth, post=True), data


def build_update_visibility_request(
    auth: Any,
    tid: str,
    content: str,
    visible: int,
    target_uins: Any = (),
    ugcright_id: str = "",
) -> QZoneRequest:
    """改一条**已发出**的说说的可见范围（`emotion_cgi_update`，v1.7.5）。

    参数与字段顺序**逐字**来自参考实现 `qzone_api/api/api_parms.py::build_edit_message_params`
    （来源见模块 docstring）。三件事必须说清：

    * `content` 是**必填**的：这条接口是"编辑说说"，服务端按整条重建——想只改可见性也得
      把当前正文原样带回去（调用方从说说列表里读到它）。传空串会把正文清掉，所以这里
      空串直接**抛 `QZoneAuthError`**（它继承 `ValueError`，属于"取参错误"那一类），
      宁可让上层报错，也不要把用户的正文擦掉。
    * 带图说说的 `richval` **无法重建**（说说列表里拿不到 `albumid` / `lloc`），这里与参考
      实现一致地发空串。所以**调用方必须先确认这条说说没有配图**再调本函数
      （`core/qzone.py` 的动作侧就是这么把关的）——否则可能把图弄丢。
    * `target_uins` 只在 `visible` 为 16（部分人可见）/ 128（部分人不可见）时有意义，
      拼成 `allow_uins`（**`|` 分隔**，见 `QZONE_ALLOW_UINS_SEPARATOR` 的说明）；其余
      三档即使给了名单也**不带**这个字段（服务端不看，带了只会让请求更容易被判非法）。
    """
    if not str(content or "").strip():
        raise QZoneAuthError("改可见范围必须带上说说正文（服务端会按整条重建，空正文等于清空）")
    uin = _auth_field(auth, "uin")
    referer = f"https://user.qzone.qq.com/{uin}"
    data: Dict[str, str] = {
        "syn_tweet_verson": "1",
        "tid": str(tid),
        "paramstr": "1",
        "pic_template": "",
        "richtype": "",
        "richval": "",
        "special_url": "",
        "subrichtype": "",
        "con": content,
        "feedversion": "1",
        "ver": "1",
        "ugc_right": str(visible),
        "to_sign": "0",
        "ugcright_id": str(ugcright_id or ""),
        "hostuin": uin,
        "code_version": "1",
        "format": "fs",
        "qzreferrer": referer,
    }
    uins = _allow_uins(target_uins) if visible in QZONE_VISIBLE_TARGETED else ""
    if uins:
        data["allow_uins"] = uins
    return "POST", _authed_url("update", auth), _headers(auth, post=True), data


def build_delete_request(
    auth: Any, tid: str, curkey: str = "", timestamp: int = 0
) -> QZoneRequest:
    """删说说。`curkey` 留空时**用 tid 兜底**（新发的说说两者相同）。"""
    uin = _auth_field(auth, "uin")
    data = {
        "uin": uin,
        "topicId": tid,
        "feedsType": "0",
        "feedsFlag": "0",
        "feedsKey": curkey or tid,
        "feedsAppid": "311",
        "feedsTime": str(timestamp),
        "fupdate": "1",
        "ref": "feeds",
        "qzreferrer": f"https://user.qzone.qq.com/{uin}",
    }
    return "POST", _authed_url("delete", auth), _headers(auth, post=True), data


def build_like_request(
    auth: Any,
    target_qq: str,
    fid: str,
    cur_key: str,
    uni_key: str,
    abstime: Optional[int] = None,
) -> QZoneRequest:
    """点赞一条说说。`abstime` 缺省 = 当前秒（照参考实现）。"""
    data = {
        "qzreferrer": f"https://user.qzone.qq.com/{target_qq}",
        "opuin": _auth_field(auth, "uin"),
        "unikey": uni_key,
        "curkey": cur_key,
        "appid": "311",
        "from": "1",
        "typeid": "0",
        "abstime": str(int(time.time()) if abstime is None else abstime),
        "fid": fid,
        "active": "0",
        "format": "json",
        "fupdate": "1",
    }
    return "POST", _authed_url("like", auth), _headers(auth, post=True), data


def build_comment_request(
    auth: Any, target_qq: str, fid: str, content: str
) -> QZoneRequest:
    """评论一条说说。`fid` 是 topicId，形状 `"{uin}_{tid}__1"`。"""
    data = {
        "topicId": fid,
        "feedsType": "100",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "plat": "qzone",
        "source": "ic",
        "hostUin": target_qq,
        "isSignIn": "",
        "platformid": "52",
        "uin": _auth_field(auth, "uin"),
        "format": "fs",
        "ref": "feeds",
        "content": content,
        "richval": "",
        "richtype": "",
        "private": "0",
        "paramstr": "1",
        "qzreferrer": f"https://user.qzone.qq.com/{target_qq}",
    }
    return "POST", _authed_url("comment", auth), _headers(auth, post=True), data


def build_forward_request(
    auth: Any, target_qq: str, tid: str, content: str = ""
) -> QZoneRequest:
    """转发一条说说，`content` 是附言。"""
    uin = _auth_field(auth, "uin")
    data = {
        "t1_uin": target_qq,
        "t1_source": "1",
        "tid": tid,
        "signin": "0",
        "con": content,
        "with_cmt": "0",
        "fwdToWeibo": "0",
        "forward_source": "2",
        "code_version": "1",
        "format": "fs",
        "hostuin": uin,
        "qzreferrer": f"https://user.qzone.qq.com/{uin}",
    }
    return "POST", _authed_url("forward", auth), _headers(auth, post=True), data


def build_feed_request(auth: Any, page: int = 1, count: int = 10) -> QZoneRequest:
    """好友动态信息流（GET）。返回的是**原始 HTML 文本**，喂 `feed_items_from_text`。"""
    params = {
        "uin": _auth_field(auth, "uin"),
        "scope": "0",
        "view": "1",
        "filter": "all",
        "flag": "1",
        "applist": "all",
        "pagenum": str(page),
        "count": str(count),
        "format": "json",
        "g_tk": str(_auth_field(auth, "g_tk")),
        "useutf8": "1",
        "outputhtmlfeed": "1",
    }
    return "GET", QZONE_URLS["feed"], _headers(auth), params


def build_mood_list_request(auth: Any, qq: str, num: int = 10) -> QZoneRequest:
    """指定好友的说说列表（GET）。返回 JSONP，喂 `parse_moods_response`。"""
    params = {
        "uin": qq,
        "ftype": "0",
        "sort": "0",
        "pos": "0",
        "num": str(num),
        "replynum": "100",
        "g_tk": str(_auth_field(auth, "g_tk")),
        "code_version": "1",
        "format": "jsonp",
        "need_private_comment": "1",
        "callback": "_preloadCallback",
    }
    return "GET", QZONE_URLS["mood_list"], _headers(auth), params


# ── C. 响应解析 ────────────────────────────────────────────────────────

_JSONP_PATTERNS = (
    r"_preloadCallback\((.*)\);?$",
    r"frameElement\.callback\((.*)\)",
    r"_Callback\((.*)\)",
    r"callback\((.*)\)",
)


def parse_jsonp(raw: Any) -> Optional[dict]:
    """解析 JSONP / 裸 JSON；**坏输入返回 `None`，绝不上抛**。

    上层（叙事/动作层）拿到的是转发链路上的任意文本：可能被网关截断、可能是腾讯的
    错误页 HTML。解析函数抛异常会让"这条动态没解析出来"升级成"整轮动作失败"，
    所以这里一律收敛成 `None`。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    for pattern in _JSONP_PATTERNS:
        m = re.search(pattern, raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except (ValueError, TypeError):
                pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, TypeError):
            pass
    return None


def hex_decode(s: str) -> str:
    """解码 `\\xNN` 转义序列（腾讯把 HTML 正文按字节转义了一次）。"""
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def _empty_feed_item() -> Dict[str, Any]:
    """`parse_feed_item` 的骨架（键与顺序 == 参考实现）。"""
    return {
        "key": "",
        "appid": "",
        "appid_name": "",
        "typeid": "",
        "opuin": "",
        "uin": "",
        "nickname": "",
        "remark": "",
        "abstime": "",
        "feedstime": "",
        "scope": "",
        "cmtnum": 0,
        "likecount": 0,
        "content": "",
        "images": [],
    }


def parse_feed_item(raw: Any) -> Dict[str, Any]:
    """从单条动态的原始文本里抠字段（照抄 `utils/feed.py::parse_feed_item`）。

    两个坑：

    * 正文与图片藏在 `html:'…'` 里，而且**先 `\\xNN` 解码、再反转义 `\\"` 与 `\\/`**
      ——顺序反了 URL 就带反斜杠；
    * 图片优先取 `data-originurl`；回退 `src=` 时那批 `qlogo`（头像）必须排掉，
      否则每条动态的"图片"里都会混进发帖人头像。

    非字符串输入（调用方切段切歪了、拿到 `None`）返回空骨架，不抛。
    """
    feed = _empty_feed_item()
    if isinstance(raw, str) and raw:
        def get(field: str) -> str:
            m = re.search(rf"{field}:\s*'([^']*)'", raw)
            if not m:
                m = re.search(rf"{field}:\s*(\d+)", raw)
            return m.group(1) if m else ""

        feed.update(
            key=get("key"),
            appid=get("appid"),
            typeid=get("typeid"),
            opuin=get("opuin"),
            uin=get("uin"),
            nickname=get("nickname"),
            remark=get("remark"),
            abstime=get("abstime"),
            feedstime=get("feedstime").strip(),
            scope=get("scope"),
        )

        cmt_m = re.search(r"cmtnum:'?(\d+)'?", raw)
        if cmt_m:
            feed["cmtnum"] = int(cmt_m.group(1))

        like_m = re.search(r"likecount:'?(\d+)'?", raw)
        if like_m:
            feed["likecount"] = int(like_m.group(1))

        html_m = re.search(r"html:'(.*?)'(?:,|\s*\})", raw, re.DOTALL)
        if html_m:
            html_content = hex_decode(html_m.group(1))
            html_content = html_content.replace('\\"', '"').replace("\\/", "/")

            infos = re.findall(
                r'class="f-info[^"]*"[^>]*>(.*?)</div>', html_content, re.DOTALL
            )
            texts = []
            for info in infos:
                clean = re.sub(r"<[^>]+>", "", info).strip()
                if clean:
                    texts.append(clean)
            feed["content"] = "\n".join(texts)

            imgs = re.findall(r'data-originurl="([^"]*)"', html_content)
            if not imgs:
                imgs = re.findall(
                    r'src="(https?://[^"]*(?:photo|pic|qlogo)[^"]*)"', html_content
                )
            feed["images"] = [u for u in imgs if "qlogo" not in u and u.strip()]
    # 未知 appid 也照参考实现写成 `未知(<appid>)`（空输入即 `未知()`），别留空串。
    feed["appid_name"] = QZONE_APP_TYPES.get(
        feed["appid"], f"未知({feed['appid']})"
    )
    return feed


def feed_items_from_text(raw: Any) -> List[Dict[str, Any]]:
    """把一整页动态文本按 `{ver:` 切段，逐段 `parse_feed_item`。

    腾讯这一页是"用 `{ver:` 伪分隔的 HTML 串"，不是合法 JSON。空 / 坏输入返回
    空列表（"这页没动态"与"这页没解析出来"在上层都表现为空——所以调用方想区分
    就得自己看原始文本长度）。
    """
    if not isinstance(raw, str) or "{ver:" not in raw:
        return []
    parts = raw.split("{ver:")
    return [parse_feed_item(item) for item in parts[1:] if item.strip()]


def parse_mood(msg: Any) -> Dict[str, Any]:
    """把说说列表里的一条原始记录整理成结构化字段（照抄 `utils/feed.py::parse_mood`）。

    宽容处理：`pic` 缺失 / 为 `None` / 元素不是 dict、`rt_con` 是字符串、`msg` 本身
    不是 dict —— 一律退化成空值，不抛。这些字段来自腾讯的接口，形状并不稳定。
    """
    item: Dict[str, Any] = {
        "tid": "",
        "content": "",
        "created_time": 0,
        "createTime": "",
        "cmtnum": 0,
        "fwdnum": 0,
        "name": "",
        "uin": "",
        "lbs": {},
        "pic": [],
        "rt_tid": "",
        "rt_uin": "",
        "rt_content": "",
    }
    if not isinstance(msg, Mapping):
        return item

    item.update(
        tid=msg.get("tid", ""),
        content=msg.get("content", ""),
        created_time=msg.get("created_time", 0),
        createTime=msg.get("createTime", ""),
        cmtnum=msg.get("cmtnum", 0),
        fwdnum=msg.get("fwdnum", 0),
        name=msg.get("name", ""),
        uin=msg.get("uin", ""),
        lbs=msg.get("lbs", {}),
    )

    for p in msg.get("pic") or []:
        if not isinstance(p, Mapping):
            continue
        item["pic"].append({
            "url": p.get("url1", ""),
            "smallurl": p.get("smallurl", ""),
            "width": p.get("width", 0),
            "height": p.get("height", 0),
        })

    rt_con = msg.get("rt_con")
    if rt_con:
        item["rt_tid"] = msg.get("rt_tid", "")
        item["rt_uin"] = msg.get("rt_uin", "")
        item["rt_content"] = rt_con.get("content", "") if isinstance(rt_con, Mapping) else ""
    return item


def parse_moods_response(text: Any) -> Dict[str, Any]:
    """JSONP → `{'code': 0, 'moods': [...]}`。

    **非 0 / 解析失败一律回空列表**，绝不把错误当成功：调用方多半按"这条说说不存在"
    继续往下走，若把 `code: -10000`（未登录）的响应当成"有动态"，上层就会拿着空
    `tid` 去点赞。`code` 原样带出来，方便调用方记审计 / 打日志。
    """
    data = parse_jsonp(text)
    if not isinstance(data, Mapping):
        return {"code": -1, "moods": []}
    code = data.get("code")
    if code != 0:
        return {"code": code if isinstance(code, int) else -1, "moods": []}
    raw_list = data.get("msglist")
    moods = raw_list if isinstance(raw_list, list) else []
    return {"code": 0, "moods": [parse_mood(msg) for msg in moods]}


def success_or_error(result: Any) -> Dict[str, Any]:
    """把写操作的 CGI 原始响应统一成 `{'success': bool, ...}`（照抄 `utils/response.py`）。

    发布 / 删除 / 点赞 / 评论 / 转发**共用这一条判定口径**：`code == 0` 才算成功
    （有的接口回 `ret`，一并认）。失败必须带 `code` 与 `message`；`message` / `msg`
    谁有取谁，都没有写 `未知错误`——上层要靠这两个字段判"能不能重试"。
    """
    if not isinstance(result, Mapping) or not result:
        return {"success": False, "code": -1, "message": "响应解析失败"}
    code = result.get("code", result.get("ret", -1))
    if code == 0:
        tid = result.get("t1_tid") or result.get("tid", "")
        out: Dict[str, Any] = {
            "success": True,
            "tid": tid,
            "time": result.get("t1_time", ""),
            "content": result.get("content", ""),
        }
        # 发布接口会返回 curkey（删说说时用；新说说与 tid 相同）。
        curkey = result.get("curkey") or result.get("t1_curkey") or ""
        if curkey:
            out["curkey"] = curkey
        return out
    return {
        "success": False,
        "code": code,
        "message": result.get("message") or result.get("msg", "未知错误"),
    }
