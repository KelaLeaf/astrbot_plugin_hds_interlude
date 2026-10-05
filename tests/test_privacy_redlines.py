"""公开文件里不得出现真实内网地址与真实账号（v1.3.4 / U16 的教训固化成测试）。

起因一：写 v1.3.4 时把用户自建 SearXNG 的**真实内网地址与端口**抄进了
`_conf_schema.json` 的 hint、README 与 CHANGELOG，而 GitHub 发布仓是公开的。
配置示例里可以写占位（`http://<地址>:<端口>/search?q={query}`），测试夹具可以写
通用私网地址（`192.168.1.9` 这种明显不是谁的机器），但**发行文件里一个都不能有**。

起因二（U16）：**真实账号**被当成测试夹具散落在 `plugin/tests/**`，同样随公开仓
发了出去。`plugin/` 就是发布仓的根——测试文件跟着发，所以账号这条红线**必须连
`tests/` 一起扫**（只扫 `tests/` 之外，等于把红线扫成瞎子：泄露恰恰发生在测试里）。
夹具一律用中性号：尾 4 位与原号相同的替身号（`1000008890` / `100001357` /
`100002551` / `100002770` / `100003135` / `100004964`），这样"保尾 4 位"的脱敏断言一字不用改。

用户真实地址与真实账号只允许留在记忆库这类私有位置。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

#: 私网地址的三种写法（RFC 1918）。刻意不查 `127.0.0.1`/`localhost`——那是回环，
#: 是安全策略里正当的"禁止"示例，不泄露任何人的内网拓扑。
PRIVATE_HOST_RE = re.compile(
    r'(?<![\w.])('
    r'192\.168\.\d{1,3}\.\d{1,3}'
    r'|10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}'
    r')(?![\w.])'
)

#: 允许出现在夹具/文档里的通用地址：`192.168.1.9`（测试专用，非任何人的机器）。
ALLOWED_FIXTURE_HOSTS = {'192.168.1.9', '192.168.1.10', '192.168.1.1'}

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'node_modules', '__pycache__', 'tests'}
TEXT_SUFFIXES = {'.py', '.json', '.md', '.yaml', '.yml', '.ts', '.tsx', '.html', '.js'}

#: 六个**真实账号**：主人本人的号、机器人 NapCat 的 self_id、另一位的号，外加 U16 收尾
#: 扫出来的两个群号（`…2770` / `…4964`）与一个用户号（`…3135`）。它们曾经被当成夹具写进
#: `plugin/tests/**`（含前端 `frontend/scripts/` 的回归脚本），仓是公开的，于是跟着发行文件
#: 一起泄露（U16）。从此这六个字面量不许出现在插件里的任何文本文件中。
#:
#: 刻意拆成两段字符串再拼：**本文件自己也在扫描范围内**，写成一整串会把自己扫红。
#: 同理，注释里也不写完整数字。
FORBIDDEN_ACCOUNT_LITERALS = (
    '210675' '8890',
    '161461' '357',
    '157677' '2551',
    '853402' '770',
    '338727' '3135',
    '746864' '964',
)

#: 用 `(?<!\d)…(?!\d)` 卡数字边界：`private:<号>` 这种嵌在串里的照样抓，
#: 又不会把更长的纯数字串（号里套号）误判成命中。
FORBIDDEN_ACCOUNT_RE = re.compile(
    r'(?<!\d)(?:' + '|'.join(FORBIDDEN_ACCOUNT_LITERALS) + r')(?!\d)'
)

#: 账号扫描**不看** `SKIP_DIRS` 里的 `tests`：真实号就是在测试夹具里泄露的。
ACCOUNT_SCAN_SKIP_DIRS = {'node_modules', '__pycache__'}


def _account_scan_files() -> list[Path]:
    """账号红线要扫的文件：`plugin/**` 全下（**含 `tests/`**），只跳第三方包与缓存。"""
    files: list[Path] = []
    for path in PACKAGE_ROOT.rglob('*'):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(PACKAGE_ROOT)
        if any(part in ACCOUNT_SCAN_SKIP_DIRS for part in relative.parts):
            continue
        files.append(path)
    return sorted(files)


def _account_offenders(paths, root: Path | None = None) -> list[str]:
    """扫出所有命中，逐条写成 `相对路径:行号 命中串`——报告里直接能定位到哪一行。"""
    offenders: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:  # pragma: no cover - 读不到就当没有
            continue
        where = path.relative_to(root) if root is not None else path
        for match in FORBIDDEN_ACCOUNT_RE.finditer(text):
            line = text[: match.start()].count('\n') + 1
            offenders.append('%s:%d %s' % (where, line, match.group(0)))
    return offenders


def _ship_files() -> list[Path]:
    """插件里会随发行版一起发出去的文件（不含 tests/ 与第三方依赖）。"""
    files: list[Path] = []
    for path in PACKAGE_ROOT.rglob('*'):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(PACKAGE_ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        files.append(path)
    return sorted(files)


class NoPrivateHostsInShippedFilesTests(unittest.TestCase):

    def test_ship_files_contain_no_private_host_literals(self):
        offenders: list[str] = []
        for path in _ship_files():
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:  # pragma: no cover - 读不到就当没有
                continue
            for match in PRIVATE_HOST_RE.finditer(text):
                host = match.group(0)
                if host in ALLOWED_FIXTURE_HOSTS:
                    continue
                line = text[: match.start()].count('\n') + 1
                offenders.append('%s:%d %s' % (path.relative_to(PACKAGE_ROOT), line, host))
        self.assertEqual(
            offenders, [],
            '发行文件里出现了内网地址（配置示例请用 http://<地址>:<端口>/... 占位）：%s' % offenders,
        )

    def test_the_guard_itself_is_not_blind(self):
        """反向断言：正则确实抓得住私网地址，别哪天把它写坏了还全绿。"""
        self.assertTrue(PRIVATE_HOST_RE.search('http://192.168.7.7:6011/search'))
        self.assertTrue(PRIVATE_HOST_RE.search('https://10.1.2.3/x'))
        self.assertTrue(PRIVATE_HOST_RE.search('https://172.20.3.4/x'))
        self.assertIsNone(PRIVATE_HOST_RE.search('https://example.com/x'))
        self.assertIsNone(PRIVATE_HOST_RE.search('http://127.0.0.1/x'), '回环地址不查')
        # 版本号不是地址：`10.29.8` 只有三段，别把依赖版本号当成内网主机。
        self.assertIsNone(PRIVATE_HOST_RE.search('"preact": "^10.29.8"'))
        self.assertIsNone(PRIVATE_HOST_RE.search('v8 10.2.154'), '依赖里的三段版本号不是地址')
        # 夹具地址同样会被正则逮到，只是扫描时按白名单放过——正则本身不该为它开口子。
        self.assertIn(PRIVATE_HOST_RE.search('http://192.168.1.9/x').group(0), ALLOWED_FIXTURE_HOSTS)


class NoRealAccountIdsInShippedFilesTests(unittest.TestCase):
    """三个真实号不许出现在插件里的任何文本文件——**包括 `tests/`**。

    `plugin/` 是发布仓的根，测试文件跟着一起发；泄露就是发生在测试夹具里的，
    所以这里单开一条扫描（范围含 `tests/`），而不是复用 `_ship_files()`。
    """

    def test_no_real_account_id_literal_anywhere_in_the_package(self):
        offenders = _account_offenders(_account_scan_files(), PACKAGE_ROOT)
        self.assertEqual(
            offenders, [],
            '发行文件里出现了真实账号（夹具请用中性替身号，保留尾 4 位）：%s' % offenders,
        )

    def test_the_scan_reaches_the_test_files_where_the_leak_happened(self):
        """反向断言：扫描范围必须包含 `tests/`，否则这条红线形同虚设。"""
        scanned = {path.relative_to(PACKAGE_ROOT).as_posix() for path in _account_scan_files()}
        self.assertIn('tests/test_privacy_redlines.py', scanned, '红线自己也得在扫描范围内')
        self.assertIn('tests/test_platform_transport.py', scanned, '泄露过的夹具文件必须在范围内')
        self.assertNotIn('__pycache__', {part for name in scanned for part in Path(name).parts})
        self.assertTrue(all(name.endswith(tuple(TEXT_SUFFIXES)) for name in scanned))

    def test_the_account_guard_itself_is_not_blind(self):
        # 三个号本身、以及嵌在 UMO / 前缀里的写法，都要抓得住。
        for literal in FORBIDDEN_ACCOUNT_LITERALS:
            with self.subTest(literal=literal):
                self.assertTrue(FORBIDDEN_ACCOUNT_RE.search(literal))
                self.assertTrue(FORBIDDEN_ACCOUNT_RE.search('private:' + literal))
                self.assertTrue(FORBIDDEN_ACCOUNT_RE.search('onebot:FriendMessage:' + literal))
                self.assertTrue(FORBIDDEN_ACCOUNT_RE.search('userId=%r' % literal))
        # 替身号（尾 4 位相同）与脱敏后的 `••••••8890` 不算命中——否则整改后的文件自己就红。
        self.assertIsNone(FORBIDDEN_ACCOUNT_RE.search('1000008890'))
        self.assertIsNone(FORBIDDEN_ACCOUNT_RE.search('100001357'))
        self.assertIsNone(FORBIDDEN_ACCOUNT_RE.search('100002551'))
        self.assertIsNone(FORBIDDEN_ACCOUNT_RE.search('•' * 6 + '8890'))
        self.assertIsNone(FORBIDDEN_ACCOUNT_RE.search('private:••••••8890'))
        # 别把"号里套号"误判：紧挨着数字的更长数字串不是这三个号。
        self.assertIsNone(FORBIDDEN_ACCOUNT_RE.search('2' + FORBIDDEN_ACCOUNT_LITERALS[0]))
        self.assertIsNone(FORBIDDEN_ACCOUNT_RE.search(FORBIDDEN_ACCOUNT_LITERALS[0] + '7'))
        self.assertIsNone(FORBIDDEN_ACCOUNT_RE.search('10000088901'))

    def test_the_list_covers_the_three_ids_found_in_the_u16_sweep(self):
        """U16 收尾补进来的两个群号 + 一个用户号：少任何一个，红线就漏一个。

        刻意用同样的两段拼接写法写这三个号——本文件在扫描范围内，写整串就把自己扫红。
        """
        self.assertEqual(len(FORBIDDEN_ACCOUNT_LITERALS), 6)
        self.assertEqual(len(set(FORBIDDEN_ACCOUNT_LITERALS)), 6, '别写重复项')
        for literal in ('853402' '770', '338727' '3135', '746864' '964'):
            with self.subTest(literal=literal):
                self.assertIn(literal, FORBIDDEN_ACCOUNT_LITERALS)

    def test_the_account_guard_reports_the_file_and_the_line(self):
        """把号写进临时文件的第 3 行 → 报错必须点名"哪个文件哪一行"。"""
        body = 'a = 1\nb = 2\nuid = "%s"\n' % FORBIDDEN_ACCOUNT_LITERALS[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'leak.py'
            path.write_text(body, encoding='utf-8')
            offenders = _account_offenders([path])
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)
        self.assertTrue(offenders[0].endswith(FORBIDDEN_ACCOUNT_LITERALS[0]), offenders)
