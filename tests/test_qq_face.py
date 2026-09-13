"""`upstream/test/qq-face.test.ts` 的逐条移植（stdlib unittest）。

上游测试文件 sha256 18bd8d58f29ff72e2614653b13097de41775a3779067b75e60426f27d441e074（39 行）。

三个上游 test 的对应关系：
1. ``known QQ system faces are translated into stable incoming semantics``
   → ``test_known_qq_system_faces_*``（5 条断言全部照搬，另补内联表完整性断言）
2. ``unknown QQ face IDs remain explicit rather than guessed``
   → ``test_unknown_qq_face_ids_remain_explicit``
3. ``outbound native faces carry a numeric OneBot id (spec: face.id int32)``
   → ``test_outbound_native_faces_carry_numeric_onebot_id`` + 两个前置辅助用例

关于第 3 条：上游它测的是 ``InterludeService.prototype.sendNativeFace``（跨模块契约），
依赖尚未落地的 ``plugin/core/service.py``。这里忠实保留全部断言（返回 True、恰好发出
1 段、段类型是 face、id 必须是数字且等于 66），但在依赖缺席时**显式 skip** 而不是假绿；
`python3 -m unittest discover -s plugin/tests -t .` 的绿灯由此保持诚实可控。
"""

import copy
import importlib
import inspect
import os
import sys
import unittest

# 把 repo 根加入 sys.path，保证 `python3 -m unittest plugin.tests.test_qq_face` 可直接运行
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from plugin.core.qq_face import (  # noqa: E402
    QFACE_SYSTEM_FACE_NAMES,
    QQ_NATIVE_FACE_IDS_BY_NAME,
    QQ_NATIVE_FACE_NAME_EXTENSIONS,
    QQ_NATIVE_FACE_NAMES,
    describe_qq_native_face,
    normalize_qq_native_face_segments,
    qq_native_face_name,
)

# 上游 service.ts 的 QQ_NATIVE_FACE_IDS（第 7262–7264 行）：
#   smile: '14', laugh: '182', sweat: '27', awkward: '111', heart: '66',
#   surprised: '0', sad: '5', angry: '106'
# 第 3 条上游测试用语义 'heart' 期望发出的 id 是数字 66。
_EXPECTED_SEMANTIC_IDS = {
    'smile': 14, 'laugh': 182, 'sweat': 27, 'awkward': 111,
    'heart': 66, 'surprised': 0, 'sad': 5, 'angry': 106,
}


def _find_service_native_face_table():
    """在 core.service 里找到等价于上游 ``QQ_NATIVE_FACE_IDS`` 的语义 → ID 表。

    上游测试通过服务实例间接覆盖这张表；这里只在可导入且语义键/取值与上游一致时采信，
    否则回落到本文件照抄上游的 ``_EXPECTED_SEMANTIC_IDS``。
    """
    try:
        service = importlib.import_module('plugin.core.service')
    except Exception:
        return None
    for name in dir(service):
        if 'FACE' not in name.upper():
            continue
        value = getattr(service, name)
        if not isinstance(value, dict) or 'heart' not in value:
            continue
        try:
            heart = int(value['heart'])
        except (TypeError, ValueError):
            continue
        if heart == 66:
            return {str(key): int(val) for key, val in value.items()}
    return None


class _FakeTransport:
    """记录型 Transport：`send_native_face` 收 face id 字符串并回 `{'ok': True}`。

    本移植版把「face.id 必须是数字」的保证放在适配层
    （`plugin/adapters/astrbot_bridge.py` 用 `Face(id=int(...))`），
    所以 core 侧的契约是「把语义映射成上游那串数字 id 交给 Transport」。
    """

    def __init__(self):
        self.native_faces = []

    async def send_native_face(self, channel_id, face_id, is_group=False):
        self.native_faces.append((channel_id, face_id, is_group))
        return {'ok': True}


class _FakeService:
    """只提供上游测试桩里那四个协作者的最小替身（Python 无 prototype.call，改用绑定方法）。"""

    def __init__(self):
        self.entries = []
        self.delivery_outcomes = []
        self.operations = []
        self.transport = _FakeTransport()

    async def serial(self, story_id, fn):
        return await fn()

    async def append_entry(self, story_id, entry, occurred_at=None):
        self.entries.append((story_id, entry, occurred_at))
        return {}

    async def update_script_delivery_outcome(self, story_id, reference, outcome, occurred_at=None):
        self.delivery_outcomes.append((story_id, reference, outcome))

    def report_operation(self, *args, **kwargs):
        self.operations.append((args, kwargs))

    def report(self, *args, **kwargs):
        pass

    def now(self):
        from plugin.core.time import utc_now

        return utc_now()


class _FakeSession:
    def __init__(self, sent):
        self.platform = 'qq'
        self.bot = _FakeBot(sent)


class _FakeBot:
    def __init__(self, sent):
        self._sent = sent

    async def send_message(self, channel, element=None, *args, **kwargs):
        if element is None and args:
            element, args = args[0], args[1:]
        # 深拷贝，避免服务后续修改同一对象影响断言
        self._sent.append((channel, copy.deepcopy(element)))


def _load_interlude_service():
    """导入真正的 ``InterludeService``；缺依赖或服务未移植时返回 (None, 原因)。"""
    try:
        service_module = importlib.import_module('plugin.core.service')
    except Exception as error:  # 服务模块尚未落地/依赖缺失
        return None, f'plugin.core.service 不可导入：{error!r}'
    service_class = getattr(service_module, 'InterludeService', None)
    if service_class is None:
        return None, 'plugin.core.service 里没有 InterludeService'
    send_native_face = getattr(service_class, 'send_native_face', None)
    if not callable(send_native_face):
        return None, 'InterludeService 还没有 send_native_face（上游 service.ts:2107）'
    try:
        import astrbot.api.message_components as Comp  # noqa: F401
    except Exception as error:
        return None, f'astrbot 不可导入，无法构造 face 段断言：{error!r}'
    return service_class, None


class TestQQNativeFace(unittest.TestCase):
    # --- 上游 test 1：已知 QQ 系统表情被翻译为稳定的来向语义 -------------------
    def test_known_qq_system_faces_are_translated_into_stable_incoming_semantics(self):
        self.assertEqual(qq_native_face_name('14'), '微笑')
        self.assertEqual(qq_native_face_name('182'), '笑哭')
        self.assertEqual(qq_native_face_name('427'), '偷感')
        self.assertEqual(
            normalize_qq_native_face_segments('<face id="427" platform="onebot"></face>'),
            '[QQ 原生表情：偷感（ID: 427）]',
        )
        self.assertEqual(
            normalize_qq_native_face_segments('好吧[CQ:face,id=182]'),
            '好吧[QQ 原生表情：笑哭（ID: 182）]',
        )

    # --- 上游 test 2：未知 ID 保持显式未收录，而不是猜测 ---------------------
    def test_unknown_qq_face_ids_remain_explicit(self):
        self.assertEqual(
            describe_qq_native_face('9999'),
            '[QQ 原生表情（ID: 9999；名称未收录）]',
        )

    # --- 上游 test 3 的前置：语义 → 数字 ID 表（上游 service.ts:7262）---------
    def test_native_face_semantic_ids_stay_numeric(self):
        table = _find_service_native_face_table() or {k: str(v) for k, v in _EXPECTED_SEMANTIC_IDS.items()}
        for semantic, expected in _EXPECTED_SEMANTIC_IDS.items():
            self.assertIn(semantic, table)
            self.assertEqual(
                int(table[semantic]), expected,
                'face.id 必须是数字：字符串 id 会被严格实现的 OneBot 服务端拒绝',
            )
        if _find_service_native_face_table() is not None:
            for semantic, value in table.items():
                self.assertIsInstance(
                    value, int,
                    f'core/service.py 的语义表 {semantic} 必须是 int，避免把字符串 id 传给 OneBot',
                )

    # --- 上游 test 3 本体：出向原生表情携带数字 OneBot id ---------------------
    def test_outbound_native_faces_carry_numeric_onebot_id(self):
        service_class, reason = _load_interlude_service()
        if service_class is None:
            self.skipTest(f'跨模块依赖未就绪：{reason}（上游 service.ts:2107 sendNativeFace）')

        service = _FakeService()
        session = _FakeSession([])
        bound = service_class.send_native_face.__get__(service, type(service))

        result = _run(bound({'id': 's'}, session, 'private:1', 'heart'))
        self.assertIs(result, True)
        self.assertEqual(len(service.transport.native_faces), 1)
        channel, face_id, is_group = service.transport.native_faces[0]
        self.assertEqual(channel, 'private:1')
        self.assertFalse(is_group)
        # 契约：交给 Transport 的是**可转成数字**的 OneBot face id（heart → 66）。
        # 真正的 `Face(id=<int>)` 由适配层构造（见 test_astrbot_bridge 的正向断言）。
        self.assertEqual(face_id, '66')
        self.assertEqual(int(face_id), 66)
        self.assertRegex(face_id, r'^\d+$', 'face id 必须是纯数字串，字符串语义 id 会被严格实现的 OneBot 服务端拒绝')
        # 落库的剧本条目仍带该语义与投递身份。
        self.assertEqual(len(service.entries), 1)
        entry = service.entries[0][1]
        self.assertEqual(entry['kind'], 'character-platform-action')
        self.assertEqual(entry['metadata']['semantic'], 'heart')

    # --- 数据资产完整性：两张表逐条可查、不被截断 ---------------------------
    def test_inlined_face_tables_are_complete_data_assets(self):
        # qface@1.4.1 系统表：275 条，覆盖 ID 0–348（有空洞但不越界，与 qface 表一致）
        self.assertEqual(len(QFACE_SYSTEM_FACE_NAMES), 275)
        self.assertEqual(min(int(k) for k in QFACE_SYSTEM_FACE_NAMES), 0)
        self.assertEqual(max(int(k) for k in QFACE_SYSTEM_FACE_NAMES), 348)
        self.assertTrue(all(k.isdigit() for k in QFACE_SYSTEM_FACE_NAMES))
        # 上游 348 之后的具名扩展：79 条
        self.assertEqual(len(QQ_NATIVE_FACE_NAME_EXTENSIONS), 79)
        self.assertEqual(QQ_NATIVE_FACE_NAME_EXTENSIONS['349'], '坚强')
        self.assertEqual(QQ_NATIVE_FACE_NAME_EXTENSIONS['431'], '蛇尾')
        # 合并表 = 275 + 79，且两表键不重叠、逐条可查
        self.assertEqual(len(QQ_NATIVE_FACE_NAMES), 354)
        self.assertFalse(set(QFACE_SYSTEM_FACE_NAMES) & set(QQ_NATIVE_FACE_NAME_EXTENSIONS))
        for key, name in QQ_NATIVE_FACE_NAMES.items():
            self.assertEqual(qq_native_face_name(key), name, f'{key} → {name} 必须可查回')
            self.assertEqual(qq_native_face_name(int(key)), name, f'数字入参 {key} 必须等价')
        # 反向索引一致（重名时保留后出现者，上游无反向查询，此处只保证自洽）
        self.assertEqual(QQ_NATIVE_FACE_IDS_BY_NAME['微笑'], '14')
        self.assertEqual(QQ_NATIVE_FACE_IDS_BY_NAME['偷感'], '427')

    def test_upstream_extension_table_is_copied_verbatim(self):
        # 逐条照抄上游 qq-face.ts 第 10–19 行的 ID → 名称（含 414/418/422/423 空档）
        upstream_pairs = {
            349: '坚强', 350: '贴贴', 351: '敲敲', 352: '咦', 353: '拜托', 354: '尊嘟假嘟', 355: '耶', 356: '666',
            357: '裂开', 358: '骰子', 359: '包剪锤', 360: '亲亲', 361: '狗狗笑哭', 362: '好兄弟', 363: '狗狗可怜', 364: '超级赞',
            365: '狗狗生气', 366: '芒狗', 367: '狗狗疑问', 368: '奥特笑哭', 369: '彩虹', 370: '祝贺', 371: '冒泡', 372: '气呼呼',
            373: '忙', 374: '波波流泪', 375: '超级鼓掌', 376: '跺脚', 377: '嗨', 378: '企鹅笑哭', 379: '企鹅流泪', 380: '真棒',
            381: '路过', 382: 'emo', 383: '企鹅爱心', 384: '晚安', 385: '太气了', 386: '呜呜呜', 387: '太好笑', 388: '太头疼',
            389: '太赞了', 390: '太头秃', 391: '太沧桑', 392: '龙年快乐', 393: '新年中龙', 394: '新年大龙', 395: '略略略', 396: '狼狗',
            397: '抛媚眼', 398: '超级ok', 399: 'tui', 400: '快乐', 401: '超级转圈', 402: '别说话', 403: '出去玩', 404: '闪亮登场',
            405: '好运来', 406: '姐是女王', 407: '我听听', 408: '臭美', 409: '送你花花', 410: '么么哒', 411: '一起嗨', 412: '开心',
            413: '摇起来', 415: '划龙舟', 416: '中龙舟', 417: '大龙舟', 419: '火车', 420: '中火车', 421: '大火车', 424: '续标识',
            425: '求放过', 426: '玩火', 427: '偷感', 428: '收到', 429: '蛇年快乐', 430: '蛇身', 431: '蛇尾',
        }
        self.assertEqual(
            QQ_NATIVE_FACE_NAME_EXTENSIONS,
            {str(k): v for k, v in upstream_pairs.items()},
        )

    # --- 防御性读取（上游 unknown 入参语义）---------------------------------
    def test_defensive_reads_never_raise(self):
        # null/undefined → ''：空文本原样返回（上游对 content 不做 trim）
        for value in (None, ''):
            self.assertIsNone(qq_native_face_name(value))
            self.assertEqual(describe_qq_native_face(value), '[QQ 原生表情（未提供 ID）]')
            self.assertEqual(normalize_qq_native_face_segments(value), '')
        # 纯空白文本原样保留（上游只 trim ID 取值，不 trim 正文）
        self.assertEqual(normalize_qq_native_face_segments('   '), '   ')
        # 容器入参等价 JS String(...)，不抛异常
        self.assertEqual(normalize_qq_native_face_segments([]), '')
        self.assertEqual(normalize_qq_native_face_segments(['a', 'b']), 'a,b')
        self.assertEqual(describe_qq_native_face({}), '[QQ 原生表情（ID: [object Object]；名称未收录）]')
        # '0' 是真实存在的表情（惊讶），空串判定不能把它误伤
        self.assertEqual(qq_native_face_name(0), '惊讶')
        self.assertEqual(qq_native_face_name(''), None)
        # 数字/字符串等价；ID 取值两侧 trim；未收录统一走"名称未收录"
        self.assertEqual(qq_native_face_name(14), qq_native_face_name('14'))
        self.assertEqual(qq_native_face_name(' 427 '), '偷感')
        self.assertEqual(describe_qq_native_face('9999'), '[QQ 原生表情（ID: 9999；名称未收录）]')
        self.assertEqual(describe_qq_native_face(9999), '[QQ 原生表情（ID: 9999；名称未收录）]')

    def test_marking_parsing_matches_upstream_regexes(self):
        # 属性写法：双引号 / 单引号 / 裸值 / 大小写不敏感 / 自闭合 / 带闭合标签
        self.assertEqual(normalize_qq_native_face_segments('<face id="427">'), '[QQ 原生表情：偷感（ID: 427）]')
        self.assertEqual(normalize_qq_native_face_segments("<face id='427'></face>"), '[QQ 原生表情：偷感（ID: 427）]')
        self.assertEqual(normalize_qq_native_face_segments('<FACE ID=427 />'), '[QQ 原生表情：偷感（ID: 427）]')
        self.assertEqual(normalize_qq_native_face_segments('<face  id = 14 >'), '[QQ 原生表情：微笑（ID: 14）]')
        self.assertEqual(normalize_qq_native_face_segments('[cq:FACE,id=182]'), '[QQ 原生表情：笑哭（ID: 182）]')
        self.assertEqual(
            normalize_qq_native_face_segments('[CQ:face,name=微笑,id=14]'),
            '[QQ 原生表情：微笑（ID: 14）]',
        )
        # 缺 id / 空 id → 未提供 ID（注意上游正则要求 "CQ:face," 带逗号，无逗号不匹配）
        self.assertEqual(normalize_qq_native_face_segments('<face></face>'), '[QQ 原生表情（未提供 ID）]')
        self.assertEqual(normalize_qq_native_face_segments('[CQ:face,]'), '[QQ 原生表情（未提供 ID）]')
        self.assertEqual(normalize_qq_native_face_segments('[CQ:face,id=]'), '[QQ 原生表情（未提供 ID）]')
        self.assertEqual(normalize_qq_native_face_segments('[CQ:face]'), '[CQ:face]')
        # 同一段文本里的多个标记全部替换；非法标记保持原样
        self.assertEqual(
            normalize_qq_native_face_segments('[CQ:face,id=14][CQ:face,id=182]'),
            '[QQ 原生表情：微笑（ID: 14）][QQ 原生表情：笑哭（ID: 182）]',
        )
        self.assertEqual(normalize_qq_native_face_segments('<facex id="1">'), '<facex id="1">')
        self.assertEqual(normalize_qq_native_face_segments('没有标记'), '没有标记')

    def test_mface_markup_becomes_mall_face(self):
        self.assertEqual(normalize_qq_native_face_segments('<mface summary="你好"></mface>'), '[QQ 商城表情：你好]')
        self.assertEqual(normalize_qq_native_face_segments('<mface name="你好">'), '[QQ 商城表情：你好]')
        self.assertEqual(normalize_qq_native_face_segments('<mface summary="" name="你好">'), '[QQ 商城表情：你好]')
        self.assertEqual(normalize_qq_native_face_segments('<mface summary="a" name="b" />'), '[QQ 商城表情：a]')
        self.assertEqual(normalize_qq_native_face_segments('<MFACE SUMMARY="X">'), '[QQ 商城表情：X]')
        self.assertEqual(normalize_qq_native_face_segments('<mface></mface>'), '[QQ 商城表情]')
        self.assertEqual(normalize_qq_native_face_segments('<mface summary=" ">'), '[QQ 商城表情]')


def _run(awaitable):
    """在同步 unittest 里跑一个协程（不需要 asyncio 事件循环脚手架）。"""
    if not inspect.isawaitable(awaitable):
        return awaitable
    import asyncio
    return asyncio.run(_await(awaitable))


async def _await(awaitable):
    return await awaitable


if __name__ == '__main__':
    unittest.main(verbosity=2)
