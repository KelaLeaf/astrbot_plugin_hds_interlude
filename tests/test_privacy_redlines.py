"""公开仓库里的隐私红线（v1.3.4 / U16 / §102 的教训固化成测试）。

起因一（v1.3.4）：把用户自建 SearXNG 的**真实内网地址与端口**抄进了
`_conf_schema.json` 的 hint、README 与 CHANGELOG，而 GitHub 发布仓是公开的。
配置示例里可以写占位（`http://<地址>:<端口>/search?q={query}`），测试夹具可以写
通用私网地址（`192.168.1.9` 这种明显不是谁的机器），但**发行文件里一个都不能有**。

起因二（U16）：**真实账号**被当成测试夹具散落在 `plugin/tests/**`，同样随公开仓
发了出去。`plugin/` 就是发布仓的根——测试文件跟着发，所以红线**必须连 `tests/`
一起扫**（只扫 `tests/` 之外，等于把红线扫成瞎子：泄露恰恰发生在测试里）。
夹具一律用中性号：尾 4 位与原号相同的替身号，这样"保尾 4 位"的脱敏断言一字不用改。

起因三（§102）：上面两条红线都只认**已知的坏字面量**，于是同一个坑换张脸又踩一次——
20 个测试文件的 docstring 里写着 `cd <某个私有绝对路径>`，`test_console_api.py` 里
写着本机 Ollama 的端口。**按"已经泄露过的那几个字面量"列黑名单，永远只追得上上一仗。**
§102 把红线换成**按形态判**的通用门禁：

1. **私有绝对路径**——`/home/`、`/Users/`、`/root/` 下的具体目录、本机 agent 状态目录、
   Windows 用户目录；
2. **私网地址**——RFC 1918 三段（既有口径）；
3. **私网 / 回环端点**——回环或私网主机**接上一个本机服务端口**：这种"一台真实机器上
   某个真实服务"的写法正是 §102 泄露的形态；
4. **裸的本机服务端口**——本机 Ollama 默认的那两个端口号（就是这次泄露的那两个数字）；
5. **凭证形态**——`ghp_` / `sk-` / `xoxb-` / `AKIA` / 私钥头等；
6. **凭证上下文里的十六进制串**——凭证字段名后面跟着 32+ 位 hex；
7. **凭证赋值且值不像占位**——凭证字段名后面跟着一个真值；
8. **裸的 32+ 位十六进制串**——只放行白名单里已核实为"内容哈希"的那几条。

每条门禁都逐条报 `文件:行号`，每条都配一条**反向自检**（把坏字面量塞进临时文件 →
必须红）。没有反向自检的门禁，"正则被写坏 / 扫描范围被改窄"这类 bug 会一路绿灯，
而它们的失败方向恰恰是**悄悄放行**。

扫描范围：`plugin/**` 全下（**含 `tests/`**），只跳 `node_modules/` 与 `__pycache__/`。
**本文件自己也在扫描范围内**——所以本文件里的探针字面量一律用 `_probe()` 拼接，
写整串会把自己扫红（账号那组沿用了同样的手法）。

用户真实地址、真实账号、私有路径只允许留在记忆库这类私有位置。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

#: 扫描范围：`plugin/**` 全下，只跳第三方包与字节码缓存。
#: **刻意不排除 `tests/`**——U16 与 §102 两次泄露都发生在测试文件里。
SKIP_DIRS = {'node_modules', '__pycache__'}

#: 当文本扫的后缀。构建产物（`.js` / `.css`）、前端脚本（`.mjs` / `.cjs`）、
#: 锁文件与 i18n 的 JSON 都在内：它们同样随发行版发出去。
TEXT_SUFFIXES = frozenset({
    '.py', '.pyi', '.json', '.json5', '.md', '.yaml', '.yml',
    '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.html', '.css',
    '.txt', '.toml', '.sh', '.xml', '.cfg', '.ini',
})


def _probe(*parts: str) -> str:
    """拼出探针字面量：**本文件自己也在扫描范围内**，写整串会把自己扫红。"""
    return ''.join(parts)


def _label(path: Path) -> str:
    """报告里用相对路径；临时文件（反向自检用）在包外，退回绝对路径。"""
    try:
        return path.relative_to(PACKAGE_ROOT).as_posix()
    except ValueError:
        return str(path)


def _scan_files() -> list[Path]:
    """红线要扫的文件：`plugin/**` 全下（**含 `tests/`**），只跳第三方包与缓存。"""
    files: list[Path] = []
    for path in PACKAGE_ROOT.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(PACKAGE_ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        files.append(path)
    return sorted(files)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:  # pragma: no cover - 读不到就当没有
        return None


def _collect(paths, regex, decide=None) -> list[str]:
    """扫出所有命中，逐条写成 `相对路径:行号 命中串`——报告里直接能定位到哪一行。

    `decide(match)` 返回 `None` 表示这条按白名单放过，否则返回要报出去的命中串。
    """
    offenders: list[str] = []
    for path in paths:
        text = _read(path)
        if text is None:  # pragma: no cover - 读不到就当没有
            continue
        for match in regex.finditer(text):
            verdict = match.group(0) if decide is None else decide(match)
            if verdict is None:
                continue
            line = text[: match.start()].count('\n') + 1
            offenders.append('%s:%d %s' % (_label(path), line, verdict))
    return offenders


def _probe_offenders(probe_line: str, regex, decide=None) -> list[str]:
    """反向自检的夹具：把一行坏字面量写进临时文件，返回命中报告。"""
    body = 'line one = 1\nline two = 2\n%s\n' % probe_line
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'leak.py'
        path.write_text(body, encoding='utf-8')
        return _collect([path], regex, decide)


# ---------------------------------------------------------------------------
# 门禁 1：私有绝对路径
# ---------------------------------------------------------------------------

#: 各类系统上"某个具体的人的家目录"。要求斜杠后至少一个路径字符，
#: 所以注释里写 `/home/`、`/root/` 这种**光杆前缀**不会命中（它没点名是谁）。
PRIVATE_PATH_RE = re.compile(
    r'(?<![\w.~/-])('
    r'/home/[A-Za-z0-9._-]+'
    r'|/Users/[A-Za-z0-9._-]+'
    r'|/root/[A-Za-z0-9._-]+'
    r'|~/\.dsh(?:/[A-Za-z0-9._-]+)*'
    r'|[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+'
    r')'
)

#: 私有路径允许清单。**只留极少数通用示例，且每条必须写明理由**（有专项用例钉住）。
ALLOWED_PRIVATE_PATHS = {
    '/root/sub': (
        '测试夹具里的**假**目录名：test_astrbot_bridge 的 list_sticker_files 用例只验证'
        '"按扩展名过滤 + 递归深度"，与任何真实路径无关——它不是谁的机器上的目录'
    ),
}


def _private_path_offenders(paths) -> list[str]:
    return _collect(
        paths, PRIVATE_PATH_RE,
        lambda match: None if match.group(0) in ALLOWED_PRIVATE_PATHS else match.group(0),
    )


# ---------------------------------------------------------------------------
# 门禁 2：私网地址（RFC 1918）
# ---------------------------------------------------------------------------

PRIVATE_HOST_RE = re.compile(
    r'(?<![\w.])('
    r'192\.168\.\d{1,3}\.\d{1,3}'
    r'|10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}'
    r')(?![\w.])'
)

#: 允许出现在夹具里的通用私网地址。判据是"明显不是谁的机器"：
#: 要么是测试专用段里的整地址，要么是安全策略用例里的通用示例。
#: **这里只放清单本身，不放正则**——正则本身不为它们开口子（有反向断言钉住）。
ALLOWED_FIXTURE_HOSTS = {
    '192.168.1.9': '既有口径的通用夹具地址（自建 SearXNG 的占位写法，不是谁的机器）',
    '192.168.1.10': '同上：与 192.168.1.9 成对的通用夹具地址',
    '192.168.1.1': '同上：家用网关的通用示例，出现在"私网一律拒绝"的闸门用例里',
    '10.0.0.5': 'SSRF / 主机闸门用例里的通用私网示例，不是真机',
    '172.20.3.4': '同上：172.16/12 段的通用私网示例，不是真机',
}


def _private_host_offenders(paths) -> list[str]:
    return _collect(
        paths, PRIVATE_HOST_RE,
        lambda match: None if match.group(0) in ALLOWED_FIXTURE_HOSTS else match.group(0),
    )


# ---------------------------------------------------------------------------
# 门禁 3：私网 / 回环主机 + 本机服务端口
# ---------------------------------------------------------------------------

#: 本机（或局域网里某台机器）真正会跑的服务端口。**只有当它接在一个回环/私网主机
#: 后面才算泄露**——那是一个"真实存在的端点"，正是 §102 泄露的形态。
#: 裸数字（`8000` / `3000`）不在此列，理由见下面 `BARE_LOCAL_PORT_RE` 的注释。
#:
#: Ollama 那两个端口**拆成两段写**：本文件在扫描范围内，写整串会把自己扫红。
_OLLAMA_PORTS = ('114' '34', '114' '36')

KNOWN_LOCAL_SERVICE_PORTS = frozenset({
    6185,                  # AstrBot WebUI
    8080, 8095, 8000, 9000, 3000, 5000, 7860,   # 常见自建 Web / API
    9222,                  # Chrome 远程调试
    6379, 5432, 3306,      # Redis / PostgreSQL / MySQL
}) | frozenset(int(port) for port in _OLLAMA_PORTS)

PRIVATE_ENDPOINT_RE = re.compile(
    r'(?<![\w.-])('
    r'127\.0\.0\.1'
    r'|localhost'
    r'|0\.0\.0\.0'
    r'|\[::1\]'
    r'|192\.168\.\d{1,3}\.\d{1,3}'
    r'|10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}'
    r')(?::(\d{1,5}))(?![\d])'
)


def _is_leaking_endpoint(match: re.Match) -> bool:
    """这条 `主机:端口` 算不算泄露。门禁与反向自检共用同一份判据。"""
    host, port = match.group(1), int(match.group(2))
    if host in ALLOWED_FIXTURE_HOSTS:
        # 夹具主机（192.168.1.9 这类）整台都是通用的，配什么端口都不算泄露。
        return False
    # 只有"本机会真跑的服务端口"才是泄密线索；`localhost:8888` 这种随便写的端口不是。
    return port in KNOWN_LOCAL_SERVICE_PORTS


def _private_endpoint_offenders(paths) -> list[str]:
    return _collect(
        paths, PRIVATE_ENDPOINT_RE,
        lambda match: match.group(0) if _is_leaking_endpoint(match) else None,
    )


# ---------------------------------------------------------------------------
# 门禁 4：裸的本机服务端口
# ---------------------------------------------------------------------------

#: 本机 Ollama 的默认端口，也是 §102 那次泄露的具体数字。
#: **只把它们当裸字面量查**：这两个数字在本项目里没有别的正当用途。
#:
#: 刻意**不**把 8080 / 6185 等也做成裸字面量门禁——实测它们在 `plugin/**` 里
#: 出现 11 次（`AstrBot WebUI 端口 6185` 的断言、`example.com:8080` 的公开 URL 用例、
#: `8000`/`3000` 之类的分页与超时上限），全都是通用数值。查了只会逼着人往白名单里
#: 堆数字，而端口这种"整个值就是字面量"的东西一旦进白名单，等于把门禁关掉。
#: 它们的**端点形态**（回环主机 + 端口）由门禁 3 负责。
BARE_LOCAL_PORT_RE = re.compile(
    r'(?<![\d.])(?:' + '|'.join(_OLLAMA_PORTS) + r')(?![\d.])'
)


# ---------------------------------------------------------------------------
# 门禁 5-8：凭证形态
# ---------------------------------------------------------------------------

#: 带厂商前缀的凭证：前缀本身就是强信号，不需要白名单。
CREDENTIAL_PREFIX_RE = re.compile(
    r'('
    r'(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{20,}'      # GitHub PAT
    r'|github_pat_[A-Za-z0-9_]{20,}'
    r'|glpat-[A-Za-z0-9_\-]{20,}'                # GitLab PAT
    r'|xox[abposr]-[A-Za-z0-9-]{10,}'            # Slack
    r'|(?:AKIA|ASIA)[0-9A-Z]{16}'                # AWS
    r'|AIza[0-9A-Za-z_\-]{35}'                   # Google API key
    r'|npm_[A-Za-z0-9]{36}'                      # npm token
    r'|sk-[A-Za-z0-9_\-]{20,}'                   # OpenAI 及同类
    r'|ya29\.[A-Za-z0-9_\-]{20,}'                # Google OAuth
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----'       # 私钥整块
    r')'
)

#: 凭证字段名（赋值左边的名字）。`key` 单独出现太常见，只认 `api_key` 这种带限定的。
_CREDENTIAL_NAME = (
    r'(?:token|secret|password|passwd|pwd|api[_-]?key|apikey|access[_-]?key'
    r'|client[_-]?secret|private[_-]?key|auth[_-]?token|bearer[_-]?token)'
)

#: 凭证上下文里的十六进制串：`secret = '<32+ 位 hex>'`。
HEX_SECRET_RE = re.compile(
    r'(?i)\b' + _CREDENTIAL_NAME + r'\b\s*[:=]\s*[\'"`]?([0-9a-fA-F]{32,})'
)

#: 凭证赋值且值**不像占位**：`token = '<非占位>'`。
CREDENTIAL_ASSIGN_RE = re.compile(
    r'(?i)\b' + _CREDENTIAL_NAME + r'\b\s*[:=]\s*[\'"]([^\'"]{8,})[\'"]'
)

#: 占位值的写法。命中任何一个就当"这不是真凭证"放过——门禁要抓的是**真值**，
#: 而"测试里故意写一个绝不会真出现的串，再断言它不进日志"是正当用法。
PLACEHOLDER_RE = re.compile(
    r'(?i)<[^>]*>'                       # <地址> / <token> / <你的密钥>
    r'|\bYOUR[_-]|\bEXAMPLE\b|\bSAMPLE\b|\bPLACEHOLDER\b|\bCHANGE ?ME\b'
    r'|\bTODO\b|\bFIXME\b|\bFAKE\b|\bDUMMY\b|\bREDACTED\b|\bMASKED\b|\bOMITTED\b'
    r'|\bXXX+\b|\*{3,}|\.\.\.|…'
    r'|\bMUST[_-]?NOT\b|\bNOT[_-]?APPEAR\b|\bHERE\b|\bINSERT\b|\bREPLACE\b'
    r'|\bSECRET\b'                        # 字面就写 "SECRET" 的值，只能当占位
)

#: 裸的 32+ 位十六进制串。**这是唯一一条需要"字面量白名单"的门禁**：
#: 仓库里合法的 32+ 位 hex 几乎全是**内容哈希**（git 提交号、上游文件的 sha256、
#: 表情包夹具的 md5），而它们和"一串随机的十六进制密钥"在字符层面无法区分。
#: 白名单里的每一条都经过核实是哈希，并且必须写明理由（有专项用例钉住）。
BARE_HEX_RE = re.compile(r'(?<![0-9A-Za-z_])[0-9a-fA-F]{32,}(?![0-9A-Za-z_])')

ALLOWED_HASH_LITERALS = {
    '26d7533e0f5800fdff865ab2f2ad7692917e1076':
        'git 提交号：astrbot_bridge 注释里记的上游快照 commit',
    '1f115fa1ffbfad2df9c317eca06236893e1841b04f97f1fba14f1b4a251105c4':
        'sha256：core/qq_face.py 记录的上游 src/qq-face.ts 指纹',
    '18bd8d58f29ff72e2614653b13097de41775a3779067b75e60426f27d441e074':
        'sha256：tests/test_qq_face.py 记录的上游测试文件指纹',
    'e6f0f8cae70cbd897bad1f538ed92585':
        'md5：前端脚本里"表情包文件不存在"提示文案中的示例文件名',
    'E734AC389ADCCE0D94883AE67607170B':
        'md5：表情包夹具的**示例文件名**（NapCat file token 的形状），非凭证',
    'c5ad6fc27d316504aabbccddeeff00112233445566778899aabbccddeeff0011':
        '合成串：test_service_helpers 里手写的"长哈希"夹具（aabbccddeeff 段一眼是编的）',
    '2697ee60816aa3d29dbfdf24dcf975c25900a97849e4e227353077540a6872ef':
        '共同作品 CAS 的内容哈希夹具（test_works）',
    '771461607c65f52df12d8e2babbb203fe527789f39e93d8f381db953b0d9096b':
        '共同作品 CAS 的内容哈希夹具（test_works）',
    '27aaa8c0e3e128811d308d0cd61eed179237027c195aa9816802315a599d631c':
        '共同作品 CAS 的内容哈希夹具（test_works）',
    'cd4e1470f502d700a06f2edd9f168f6fe1c43f7e69ce817d5edcda10c44aa276':
        '共同作品 CAS 的内容哈希夹具（test_works）',
}


def _credential_assign_offenders(paths) -> list[str]:
    def decide(match: re.Match) -> str | None:
        if PLACEHOLDER_RE.search(match.group(1)):
            return None
        return match.group(0)

    return _collect(paths, CREDENTIAL_ASSIGN_RE, decide)


def _bare_hex_offenders(paths) -> list[str]:
    return _collect(
        paths, BARE_HEX_RE,
        lambda match: None if match.group(0) in ALLOWED_HASH_LITERALS else match.group(0),
    )


# ---------------------------------------------------------------------------
# 真实账号（U16）：本文件自己也在扫描范围内，所以字面量拆成两段再拼
# ---------------------------------------------------------------------------

#: 六个**真实账号**：主人本人的号、机器人 NapCat 的 self_id、另一位的号，外加 U16 收尾
#: 扫出来的两个群号与一个用户号。它们曾经被当成夹具写进 `plugin/tests/**`
#: （含前端 `frontend/scripts/` 的回归脚本），仓是公开的，于是跟着发行文件一起泄露。
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


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class ScanScopeTests(unittest.TestCase):
    """扫描范围本身就是红线的一部分：范围变窄 = 门禁变瞎。"""

    def test_the_scan_reaches_the_test_files_where_the_leaks_happened(self):
        scanned = {_label(path) for path in _scan_files()}
        self.assertIn('tests/test_privacy_redlines.py', scanned, '红线自己也得在扫描范围内')
        self.assertIn('tests/test_platform_transport.py', scanned, 'U16 泄露过的夹具必须在范围内')
        self.assertIn('tests/test_console_api.py', scanned, '§102 泄露过端口的文件必须在范围内')
        self.assertIn('tests/test_database.py', scanned, '§102 泄露过私有路径的文件必须在范围内')
        self.assertIn('pages/console/index.html', scanned, '构建产物跟着发行，必须在范围内')
        self.assertIn('_conf_schema.json', scanned, '配置 schema 跟着发行，必须在范围内')
        self.assertIn('.astrbot-plugin/i18n/zh-CN.json', scanned, 'i18n 文案跟着发行')

    def test_the_scan_skips_third_party_and_bytecode(self):
        parts = {part for path in _scan_files() for part in Path(_label(path)).parts}
        self.assertNotIn('node_modules', parts)
        self.assertNotIn('__pycache__', parts)

    def test_the_scan_only_reads_known_text_suffixes(self):
        for path in _scan_files():
            self.assertIn(path.suffix.lower(), TEXT_SUFFIXES, path)


class NoPrivateAbsolutePathsTests(unittest.TestCase):
    """门禁 1：私有绝对路径不许进发行文件。"""

    def test_no_private_absolute_path_in_the_package(self):
        offenders = _private_path_offenders(_scan_files())
        self.assertEqual(
            offenders, [],
            '发行文件里出现了私有绝对路径（运行命令请写成与路径无关的形式，'
            '例如 `python3 -m unittest plugin.tests.test_x -v`）：%s' % offenders,
        )

    def test_the_path_guard_reports_the_file_and_the_line(self):
        leak = _probe('/home/', 'someone/private/repo')
        offenders = _probe_offenders('cmd = "%s"' % leak, PRIVATE_PATH_RE)
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)
        self.assertTrue(leak.startswith(offenders[0].rsplit(' ', 1)[-1]), offenders)

    def test_the_path_guard_catches_every_system(self):
        for leak in (
            _probe('/home/', 'someone/repo'),
            _probe('/Users/', 'someone/repo'),
            _probe('/root/', '.ssh/id_rsa'),
            _probe('~/', '.dsh/cache/x'),
            _probe('C:\\', 'Users\\someone\\repo'),
        ):
            with self.subTest(leak=leak):
                self.assertIsNotNone(PRIVATE_PATH_RE.search(leak))

    def test_the_path_guard_does_not_fire_on_bare_prefixes_or_docs(self):
        # 注释里教"哪几类路径不许写"时，光杆前缀不该命中（它没点名是谁的家目录）。
        self.assertIsNone(PRIVATE_PATH_RE.search('# 私有绝对路径：/home/、/Users/、/root/'))
        self.assertIsNone(PRIVATE_PATH_RE.search("'/home/'"))
        self.assertIsNone(PRIVATE_PATH_RE.search('https://example.com/home/index.html'))
        self.assertIsNone(PRIVATE_PATH_RE.search('/usr/lib/python3/site-packages'))
        self.assertIsNone(PRIVATE_PATH_RE.search('~/project'))

    def test_every_allowed_private_path_states_a_reason(self):
        self.assertLessEqual(len(ALLOWED_PRIVATE_PATHS), 3, '白名单只留极少数通用示例')
        for literal, reason in ALLOWED_PRIVATE_PATHS.items():
            with self.subTest(literal=literal):
                self.assertIsNotNone(PRIVATE_PATH_RE.search(literal), '白名单项得真能被正则命中')
                self.assertGreaterEqual(len(reason), 20, '每条白名单必须写明理由')


class NoPrivateHostsInShippedFilesTests(unittest.TestCase):
    """门禁 2：私网地址（RFC 1918）。"""

    def test_ship_files_contain_no_private_host_literals(self):
        offenders = _private_host_offenders(_scan_files())
        self.assertEqual(
            offenders, [],
            '发行文件里出现了内网地址（配置示例请用 http://<地址>:<端口>/... 占位）：%s'
            % offenders,
        )

    def test_the_host_guard_reports_the_file_and_the_line(self):
        leak = _probe('192.168.', '7.7')
        offenders = _probe_offenders('url = "http://%s:6011/search"' % leak, PRIVATE_HOST_RE)
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)
        self.assertTrue(leak.startswith(offenders[0].rsplit(' ', 1)[-1]), offenders)

    def test_the_guard_itself_is_not_blind(self):
        """反向断言：正则确实抓得住私网地址，别哪天把它写坏了还全绿。"""
        self.assertTrue(PRIVATE_HOST_RE.search('http://%s:6011/search' % _probe('192.168.', '7.7')))
        self.assertTrue(PRIVATE_HOST_RE.search('https://%s/x' % _probe('10.1.', '2.3')))
        self.assertTrue(PRIVATE_HOST_RE.search('https://%s/x' % _probe('172.20.', '3.4')))
        self.assertIsNone(PRIVATE_HOST_RE.search('https://example.com/x'))
        self.assertIsNone(PRIVATE_HOST_RE.search('https://127.0.0.1/x'), '回环地址不查')
        # 版本号不是地址：`10.29.8` 只有三段，别把依赖版本号当成内网主机。
        self.assertIsNone(PRIVATE_HOST_RE.search('"preact": "^10.29.8"'))
        self.assertIsNone(PRIVATE_HOST_RE.search('v8 10.2.154'), '依赖里的三段版本号不是地址')
        # 夹具地址同样会被正则逮到，只是扫描时按白名单放过——正则本身不该为它开口子。
        fixture = '192.168.1.9'
        self.assertIn(PRIVATE_HOST_RE.search('http://%s/x' % fixture).group(0),
                      ALLOWED_FIXTURE_HOSTS)

    def test_every_allowed_fixture_host_states_a_reason(self):
        self.assertLessEqual(len(ALLOWED_FIXTURE_HOSTS), 8, '白名单只留极少数通用示例')
        for host, reason in ALLOWED_FIXTURE_HOSTS.items():
            with self.subTest(host=host):
                self.assertTrue(PRIVATE_HOST_RE.fullmatch(host), '白名单项得真能被正则命中')
                self.assertGreaterEqual(len(reason), 20, '每条白名单必须写明理由')


class NoPrivateEndpointsTests(unittest.TestCase):
    """门禁 3：回环/私网主机接上一个本机服务端口——"真实机器的真实服务"。"""

    def test_no_private_or_loopback_endpoint_on_a_local_service_port(self):
        offenders = _private_endpoint_offenders(_scan_files())
        self.assertEqual(
            offenders, [],
            '发行文件里出现了本机/私网端点（夹具请用中性端口，例如 127.0.0.1:9）：%s' % offenders,
        )

    def test_the_endpoint_guard_reports_the_file_and_the_line(self):
        leak = _probe('127.0.0.1:', _OLLAMA_PORTS[0])
        offenders = _probe_offenders(
            'url = "http://%s/v1/chat/completions"' % leak,
            PRIVATE_ENDPOINT_RE,
            lambda match: match.group(0) if _is_leaking_endpoint(match) else None,
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)
        self.assertTrue(leak.startswith(offenders[0].rsplit(' ', 1)[-1]), offenders)

    def test_the_endpoint_guard_is_not_blind(self):
        def hits(text):
            return [m for m in PRIVATE_ENDPOINT_RE.finditer(text) if _is_leaking_endpoint(m)]

        self.assertTrue(hits('http://%s/v1' % _probe('127.0.0.1:', _OLLAMA_PORTS[0])))
        self.assertTrue(hits('http://%s/x' % _probe('localhost:', '6185')))
        self.assertTrue(hits('http://%s/x' % _probe('192.168.', '0.110:' + _OLLAMA_PORTS[1])))
        # 没有端口 / 端口不是本机服务 / 主机是公开域名 → 都不算泄露。
        self.assertFalse(hits('https://127.0.0.1/x'))
        self.assertFalse(hits('http://%s/search' % _probe('localhost:', '8888')))
        self.assertFalse(hits('http://example.com:8080/a'))
        self.assertFalse(hits('https://example.com:%s/x' % _OLLAMA_PORTS[0]))


class NoBareLocalServicePortsTests(unittest.TestCase):
    """门禁 4：裸的本机 Ollama 默认端口（数字见 `_OLLAMA_PORTS`）。"""

    def test_no_bare_ollama_port_in_the_package(self):
        offenders = _collect(_scan_files(), BARE_LOCAL_PORT_RE)
        self.assertEqual(
            offenders, [],
            '发行文件里出现了本机 Ollama 端口（夹具请用中性端口，例如 127.0.0.1:9）：%s'
            % offenders,
        )

    def test_the_bare_port_guard_reports_the_file_and_the_line(self):
        leak = _OLLAMA_PORTS[0]
        offenders = _probe_offenders('port = %s' % leak, BARE_LOCAL_PORT_RE)
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)
        self.assertTrue(leak.startswith(offenders[0].rsplit(' ', 1)[-1]), offenders)

    def test_the_bare_port_guard_is_not_blind(self):
        for port in _OLLAMA_PORTS:
            with self.subTest(port=port):
                self.assertTrue(BARE_LOCAL_PORT_RE.search('host = "%s"' % port))
                self.assertTrue(BARE_LOCAL_PORT_RE.search(port))
        # 邻位数字不算：别把更长的数字串误判成端口。
        self.assertIsNone(BARE_LOCAL_PORT_RE.search('1%s9' % _OLLAMA_PORTS[0]))
        self.assertIsNone(BARE_LOCAL_PORT_RE.search('%s0' % _OLLAMA_PORTS[0]))
        self.assertIsNone(BARE_LOCAL_PORT_RE.search('114.34'))


class NoCredentialShapedLiteralsTests(unittest.TestCase):
    """门禁 5-8：凭证形态。"""

    def test_no_credential_prefix_anywhere_in_the_package(self):
        offenders = _collect(_scan_files(), CREDENTIAL_PREFIX_RE)
        self.assertEqual(
            offenders, [],
            '发行文件里出现了带厂商前缀的凭证（令牌一律走配置，不写进源码）：%s' % offenders,
        )

    def test_no_hex_secret_in_a_credential_context(self):
        offenders = _collect(_scan_files(), HEX_SECRET_RE)
        self.assertEqual(
            offenders, [],
            '发行文件里出现了"凭证字段 = 32+ 位十六进制串"（真凭证请走配置）：%s' % offenders,
        )

    def test_no_credential_assignment_with_a_real_looking_value(self):
        offenders = _credential_assign_offenders(_scan_files())
        self.assertEqual(
            offenders, [],
            '发行文件里出现了像真凭证的赋值（占位请写成 <...> 或含 example/placeholder）：%s'
            % offenders,
        )

    def test_no_bare_hash_length_hex_outside_the_allowlist(self):
        offenders = _bare_hex_offenders(_scan_files())
        self.assertEqual(
            offenders, [],
            '发行文件里出现了 32+ 位十六进制串。若它确实是内容哈希（提交号 / sha256 / '
            '夹具 md5），把它连同理由加进 ALLOWED_HASH_LITERALS；否则它就是凭证：%s' % offenders,
        )

    def test_the_credential_guards_report_the_file_and_the_line(self):
        token = _probe('ghp_', 'A' * 36)
        offenders = _probe_offenders('token = "%s"' % token, CREDENTIAL_PREFIX_RE)
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)
        self.assertTrue(token.startswith(offenders[0].rsplit(' ', 1)[-1]), offenders)

        hex_secret = _probe('a1b2c3d4', 'e5f60718', '293a4b5c', '6d7e8f90', '12345678')
        offenders = _probe_offenders('secret = "%s"' % hex_secret, HEX_SECRET_RE)
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)

        # 探针值刻意**不像占位**（不能含 placeholder / example / <...> 这类字样），
        # 否则它会被 PLACEHOLDER_RE 放过，反向自检就变成"永远绿"的假门禁。
        # 整串拆开写：本文件在扫描范围内。
        assign = _probe('api_key', ' = "k9Xq2mZ7pL4vR8tW"')
        offenders = _probe_offenders(
            assign, CREDENTIAL_ASSIGN_RE,
            lambda m: None if PLACEHOLDER_RE.search(m.group(1)) else m.group(0),
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)

        offenders = _probe_offenders('blob = "%s"' % hex_secret, BARE_HEX_RE)
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)

    def test_the_credential_guards_are_not_blind(self):
        self.assertTrue(CREDENTIAL_PREFIX_RE.search(_probe('ghp_', 'A' * 36)))
        self.assertTrue(CREDENTIAL_PREFIX_RE.search(_probe('sk-', 'B' * 40)))
        self.assertTrue(CREDENTIAL_PREFIX_RE.search(_probe('xoxb-', '1234567890')))
        self.assertTrue(CREDENTIAL_PREFIX_RE.search(_probe('AKIA', 'ABCDEFGHIJKLMNOP')))
        self.assertTrue(CREDENTIAL_PREFIX_RE.search(_probe('-----BEGIN ', 'RSA PRIVATE KEY-----')))
        self.assertIsNone(CREDENTIAL_PREFIX_RE.search('sk-'), '光杆前缀不算')
        self.assertIsNone(CREDENTIAL_PREFIX_RE.search('task-free'), '普通英文词不误判')
        # `p_skey=` 里的 `sk` 不是 `sk-` 前缀。
        self.assertIsNone(CREDENTIAL_PREFIX_RE.search('p_skey=abc'))

        hex_secret = _probe('a1b2c3d4', 'e5f60718', '293a4b5c', '6d7e8f90', '12345678')
        self.assertTrue(HEX_SECRET_RE.search('secret = "%s"' % hex_secret))
        self.assertTrue(HEX_SECRET_RE.search("api_key: '%s'" % hex_secret))
        self.assertIsNone(HEX_SECRET_RE.search('sha256 %s' % hex_secret),
                          '没有凭证字段名的裸哈希不算')
        self.assertIsNone(HEX_SECRET_RE.search('secret = "short"'))

        self.assertIsNotNone(PLACEHOLDER_RE.search('<地址>'))
        self.assertIsNotNone(PLACEHOLDER_RE.search('YOUR_API_KEY'))
        self.assertIsNotNone(PLACEHOLDER_RE.search('example-token'))
        self.assertIsNotNone(PLACEHOLDER_RE.search('CHANGEME'))
        self.assertIsNotNone(PLACEHOLDER_RE.search('FAKE'))
        self.assertIsNotNone(PLACEHOLDER_RE.search('THIS-MUST-NOT-APPEAR-IN-LOGS'))
        self.assertIsNone(PLACEHOLDER_RE.search(hex_secret), '真随机串不该被当成占位')

    def test_every_allowed_hash_states_a_reason_and_is_a_real_hash(self):
        self.assertLessEqual(len(ALLOWED_HASH_LITERALS), 24, '白名单不能变成垃圾桶')
        for literal, reason in ALLOWED_HASH_LITERALS.items():
            with self.subTest(literal=literal):
                self.assertRegex(literal, r'^[0-9a-fA-F]{32,}$', '键就是命中的字面量本身')
                self.assertGreaterEqual(len(reason), 20, '每条白名单必须写明理由')
        # 反向：白名单**只**免掉这十条，别的 hex 照样红。
        fresh = _probe('0123456789abcdef', '0123456789abcdef', '0123456789abcdef')
        self.assertNotIn(fresh, ALLOWED_HASH_LITERALS)
        self.assertIsNotNone(BARE_HEX_RE.search(fresh))


#: 反向自检探针的文件名前缀与后缀。**后缀刻意不是 `.py`**，理由见
#: `GateMutationTests` 的类 docstring（`test_command_guards` 与
#: `test_bubble_split_newline` 会把插件下**每一个 `.py`** 都 `ast.parse` 一遍）。
PROBE_PREFIX = '_privacy_gate_probe_'
PROBE_SUFFIX = '.md'


def _sweep_probes() -> list[str]:
    """清掉反向自检留下的探针文件，返回被清掉的文件名。

    正常路径下探针由 `_assert_fires` 的 `finally` 删掉；这里兜底的是
    "上一次跑到一半被打断"留下的残留——它会让**下一次**全量把探针误判成真泄露。
    """
    removed: list[str] = []
    for stale in sorted((PACKAGE_ROOT / 'tests').glob(PROBE_PREFIX + '*' + PROBE_SUFFIX)):
        stale.unlink(missing_ok=True)
        removed.append(stale.name)
    return removed


def setUpModule() -> None:  # noqa: N802 - unittest 的模块级钩子
    """进入本模块前先清残留，别让上一次的探针污染本次全量。"""
    _sweep_probes()


class GateMutationTests(unittest.TestCase):
    """反向自检（真实范围）：把坏字面量**塞进 `plugin/**` 里**，门禁必须当场变红。

    临时文件建在 `plugin/tests/` 下、用完即删，这样验的是**真实的 `_scan_files()`
    配真实的门禁**，而不是"门禁正则单独 search 一下"。缺了这一层，"扫描范围被改窄"
    或"某条门禁压根没接进扫描"这类 bug 照样一路全绿——它们的失败方向都是悄悄放行。

    ⚠️ 探针的**后缀是 `.md` 不是 `.py`、文件名逐条用例独立**，这两条都是实测踩出来的：

    - `test_command_guards` 与 `test_bubble_split_newline` 会 `rglob('*.py')` 把
      插件下**每一个 `.py`** 都 `ast.parse` 一遍（只跳 `__pycache__`，**不跳 `tests/`**），
      探针叫 `*.py` 就会被它们当成生产代码读进去——跨用例污染。
    - 早先所有探针共用一个文件名，曾在一次全量里出现"互相覆盖 / 被别的用例提前删掉"
      的串扰（同一次运行里 5 条红）。改成逐条独立命名 + `setUp`/`tearDown`/`setUpModule`
      三重清扫后，反复全量复跑不再复现。
    """

    def setUp(self):
        _sweep_probes()

    def tearDown(self):
        _sweep_probes()

    def _probe_path(self) -> Path:
        return PACKAGE_ROOT / 'tests' / (
            '%s%s%s' % (PROBE_PREFIX, self._testMethodName, PROBE_SUFFIX)
        )

    def _write_probe(self, body_line: str) -> Path:
        path = self._probe_path()
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_text('a = 1\nb = 2\n%s\n' % body_line, encoding='utf-8')
        return path

    def _assert_fires(self, scan, body_line: str) -> None:
        path = self._write_probe(body_line)
        try:
            offenders = scan(_scan_files())
        finally:
            path.unlink(missing_ok=True)
        expected_prefix = 'tests/%s:3 ' % path.name
        self.assertTrue(
            any(item.startswith(expected_prefix) for item in offenders),
            '门禁没有抓到塞进真实扫描范围（%s 第 3 行）的坏字面量：%s' % (path.name, offenders),
        )

    def test_private_path_gate_fires_on_a_real_file(self):
        self._assert_fires(
            _private_path_offenders, 'cmd = "%s"' % _probe('/home/', 'someone/repo'),
        )

    def test_private_host_gate_fires_on_a_real_file(self):
        self._assert_fires(
            _private_host_offenders, 'url = "http://%s/x"' % _probe('192.168.', '7.7'),
        )

    def test_endpoint_gate_fires_on_a_real_file(self):
        self._assert_fires(
            _private_endpoint_offenders,
            'url = "http://%s/v1/chat/completions"' % _probe('127.0.0.1:', _OLLAMA_PORTS[0]),
        )

    def test_bare_port_gate_fires_on_a_real_file(self):
        self._assert_fires(
            lambda paths: _collect(paths, BARE_LOCAL_PORT_RE),
            'port = %s' % _OLLAMA_PORTS[0],
        )

    def test_credential_prefix_gate_fires_on_a_real_file(self):
        self._assert_fires(
            lambda paths: _collect(paths, CREDENTIAL_PREFIX_RE),
            'token = "%s"' % _probe('ghp_', 'A' * 36),
        )

    def test_hex_secret_gate_fires_on_a_real_file(self):
        self._assert_fires(
            lambda paths: _collect(paths, HEX_SECRET_RE),
            'secret = "%s"' % _probe('a1b2c3d4', 'e5f60718', '293a4b5c', '6d7e8f90', '12345678'),
        )

    def test_credential_assign_gate_fires_on_a_real_file(self):
        self._assert_fires(
            _credential_assign_offenders,
            _probe('api_key', ' = "k9Xq2mZ7pL4vR8tW"'),
        )

    def test_bare_hex_gate_fires_on_a_real_file(self):
        self._assert_fires(
            _bare_hex_offenders,
            'blob = "%s"' % _probe('0123456789abcdef', '0123456789abcdef', '0123456789abcdef'),
        )

    def test_account_gate_fires_on_a_real_file(self):
        self._assert_fires(
            lambda paths: _collect(paths, FORBIDDEN_ACCOUNT_RE),
            'uid = "%s"' % FORBIDDEN_ACCOUNT_LITERALS[0],
        )

    def test_a_benign_probe_file_fires_nothing(self):
        """反向的**反向**：一份干净文件必须一条门禁都不触发。

        没有这条，门禁写成"永远为真"（比如正则被写成 `.*`）也会被上面九条蒙混过去。
        """
        path = self._write_probe(
            'url = "https://example.com/search?q={query}"\n'
            '# host = "192.168.1.9"  # 夹具地址，白名单里\n'
            'port = 8888\n'
            'token = "<你的令牌>"'
        )
        gates = (
            ('私有路径', _private_path_offenders),
            ('私网地址', _private_host_offenders),
            ('私网端点', _private_endpoint_offenders),
            ('裸端口', lambda paths: _collect(paths, BARE_LOCAL_PORT_RE)),
            ('凭证前缀', lambda paths: _collect(paths, CREDENTIAL_PREFIX_RE)),
            ('hex 凭证', lambda paths: _collect(paths, HEX_SECRET_RE)),
            ('凭证赋值', _credential_assign_offenders),
            ('裸 hex', _bare_hex_offenders),
            ('真实账号', lambda paths: _collect(paths, FORBIDDEN_ACCOUNT_RE)),
        )
        try:
            scanned = _scan_files()
            self.assertIn(path, scanned, '探针必须真的落在扫描范围里，否则这条自检是假的')
            for name, scan in gates:
                with self.subTest(gate=name):
                    self.assertEqual(
                        scan(scanned), [], '%s 门禁把干净文件误判成泄露' % name,
                    )
        finally:
            path.unlink(missing_ok=True)

    def test_the_probe_suffix_is_not_python(self):
        """探针后缀不许是 `.py`：`test_command_guards` 等会把插件下每个 `.py` 都 AST 解析。"""
        self.assertNotEqual(PROBE_SUFFIX, '.py')
        self.assertIn(PROBE_SUFFIX, TEXT_SUFFIXES, '探针后缀必须在扫描范围内才有意义')

    def test_a_leftover_probe_is_swept_before_it_can_be_mistaken_for_a_leak(self):
        """上一次跑到一半崩了留下的探针，必须能被清扫掉。"""
        stale = PACKAGE_ROOT / 'tests' / (PROBE_PREFIX + 'leftover' + PROBE_SUFFIX)
        stale.write_text(
            'a = 1\nb = 2\ncmd = "%s"\n' % _probe('/home/', 'someone/repo'),
            encoding='utf-8',
        )
        self.assertTrue(stale.exists())
        # 先证明它确实会被门禁当真泄露（这就是必须清扫的原因）。
        self.assertTrue(_private_path_offenders([stale]), '残留探针应当被门禁抓到')
        removed = _sweep_probes()
        self.assertIn(stale.name, removed)
        self.assertFalse(stale.exists(), '残留探针必须被扫掉，否则下一次全量会误判成泄露')
        self.assertEqual(_private_path_offenders(_scan_files()), [])


class NoRealAccountIdsInShippedFilesTests(unittest.TestCase):
    """U16：真实号不许出现在插件里的任何文本文件——**包括 `tests/`**。

    `plugin/` 是发布仓的根，测试文件跟着一起发；泄露就是发生在测试夹具里的，
    所以这里单开一条扫描（范围含 `tests/`）。
    """

    def test_no_real_account_id_literal_anywhere_in_the_package(self):
        offenders = _collect(_scan_files(), FORBIDDEN_ACCOUNT_RE)
        self.assertEqual(
            offenders, [],
            '发行文件里出现了真实账号（夹具请用中性替身号，保留尾 4 位）：%s' % offenders,
        )

    def test_the_account_guard_itself_is_not_blind(self):
        # 六个号本身、以及嵌在 UMO / 前缀里的写法，都要抓得住。
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
        offenders = _probe_offenders(
            'uid = "%s"' % FORBIDDEN_ACCOUNT_LITERALS[0], FORBIDDEN_ACCOUNT_RE,
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(':3 ', offenders[0], offenders)
        self.assertTrue(FORBIDDEN_ACCOUNT_LITERALS[0].startswith(offenders[0].rsplit(' ', 1)[-1]), offenders)
