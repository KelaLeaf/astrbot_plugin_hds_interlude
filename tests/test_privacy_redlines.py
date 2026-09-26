"""公开文件里不得出现真实内网地址（v1.3.4 的教训固化成测试）。

起因：写 v1.3.4 时把用户自建 SearXNG 的**真实内网地址与端口**抄进了
`_conf_schema.json` 的 hint、README 与 CHANGELOG，而 GitHub 发布仓是公开的。
配置示例里可以写占位（`http://<地址>:<端口>/search?q={query}`），测试夹具可以写
通用私网地址（`192.168.1.9` 这种明显不是谁的机器），但**发行文件里一个都不能有**。

扫描范围是"会跟着插件发给所有人"的东西（`plugin/` 下除 `tests/` 之外的文件，
以及前端源码——`frontend/node_modules/` 是第三方包，跳过）。用户真实地址只允许
留在记忆库这类私有位置。
"""

from __future__ import annotations

import re
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
