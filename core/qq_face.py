"""QQ 原生表情 ID → 名称映射与适配器标记归一化。

上游：``upstream/src/qq-face.ts``（sha256 1f115fa1ffbfad2df9c317eca06236893e1841b04f97f1fba14f1b4a251105c4，54 行，v1.0.1-beta6-rebuild）。

依赖替换说明
------------
上游第 1 行 ``import * as qface from 'qface'`` 只用于查询 ID ≤ 348 的系统表情表，
且只取 ``qface.get(id).QDes``（形如 ``'/微笑'``）。按 PORT_PLAN「不引入第三方依赖」，
这里**不引入 qface**，改为把 ``qface@1.4.1``（``upstream/package.json`` 声明 ``"qface": "^1.4.1"``）
``lib/data.json`` 的全表逐条内联为 QFACE_SYSTEM_FACE_NAMES：275 条，覆盖 ID 0–348，
取值已按上游 ``face?.QDes?.replace(/^\\//, '').trim()`` 去掉前导斜杠并 trim。

上游第 3–8 行注释说明：QFace 的长青系统表情表只到 348，QQ 之后继续新增原生动态表情却
没有稳定的公开机器可读表，因此把较新的具名系统条目保留在 QQ_NATIVE_FACE_NAME_EXTENSIONS
（79 条，349–431，含上游原样保留的 414/418/422/423 空档）。未收录 ID 故意保持 unknown，
绝不为其臆测含义。

查表优先级与上游一致：先扩展表，再系统表。
"""

import math
import re

# ---------------------------------------------------------------------------
# QFace 系统表情表（ID 0–348，275 条）
# 数据来源：qface@1.4.1 lib/data.json 的 QSid → QDes（去前导 '/' 并 trim）。
# 这是上游 ``qface.get(key)?.QDes`` 的等价内联数据，不是新造表。
# ---------------------------------------------------------------------------
QFACE_SYSTEM_FACE_NAMES = {
    '0': '惊讶', '1': '撇嘴', '2': '色', '3': '发呆', '4': '得意', '5': '流泪', '6': '害羞', '7': '闭嘴',
    '8': '睡', '9': '大哭', '10': '尴尬', '11': '发怒', '12': '调皮', '13': '呲牙', '14': '微笑', '15': '难过',
    '16': '酷', '18': '抓狂', '19': '吐', '20': '偷笑', '21': '可爱', '22': '白眼', '23': '傲慢', '24': '饥饿',
    '25': '困', '26': '惊恐', '27': '流汗', '28': '憨笑', '29': '悠闲', '30': '奋斗', '31': '咒骂', '32': '疑问',
    '33': '嘘', '34': '晕', '35': '折磨', '36': '衰', '37': '骷髅', '38': '敲打', '39': '再见', '41': '发抖',
    '42': '爱情', '43': '跳跳', '46': '猪头', '49': '拥抱', '53': '蛋糕', '54': '闪电', '55': '炸弹', '56': '刀',
    '57': '足球', '59': '便便', '60': '咖啡', '61': '饭', '63': '玫瑰', '64': '凋谢', '66': '爱心', '67': '心碎',
    '69': '礼物', '74': '太阳', '75': '月亮', '76': '赞', '77': '踩', '78': '握手', '79': '胜利', '85': '飞吻',
    '86': '怄火', '89': '西瓜', '96': '冷汗', '97': '擦汗', '98': '抠鼻', '99': '鼓掌', '100': '糗大了', '101': '坏笑',
    '102': '左哼哼', '103': '右哼哼', '104': '哈欠', '105': '鄙视', '106': '委屈', '107': '快哭了', '108': '阴险', '109': '左亲亲',
    '110': '吓', '111': '可怜', '112': '菜刀', '113': '啤酒', '114': '篮球', '115': '乒乓', '116': '示爱', '117': '瓢虫',
    '118': '抱拳', '119': '勾引', '120': '拳头', '121': '差劲', '122': '爱你', '123': 'NO', '124': 'OK', '125': '转圈',
    '126': '磕头', '127': '回头', '128': '跳绳', '129': '挥手', '130': '激动', '131': '街舞', '132': '献吻', '133': '左太极',
    '134': '右太极', '136': '双喜', '137': '鞭炮', '138': '灯笼', '140': 'K歌', '144': '喝彩', '145': '祈祷', '146': '爆筋',
    '147': '棒棒糖', '148': '喝奶', '151': '飞机', '158': '钞票', '168': '药', '169': '手枪', '171': '茶', '172': '眨眼睛',
    '173': '泪奔', '174': '无奈', '175': '卖萌', '176': '小纠结', '177': '喷血', '178': '斜眼笑', '179': 'doge', '180': '惊喜',
    '181': '骚扰', '182': '笑哭', '183': '我最美', '184': '河蟹', '185': '羊驼', '187': '幽灵', '188': '蛋', '190': '菊花',
    '192': '红包', '193': '大笑', '194': '不开心', '197': '冷漠', '198': '呃', '199': '好棒', '200': '拜托', '201': '点赞',
    '202': '无聊', '203': '托脸', '204': '吃', '205': '送花', '206': '害怕', '207': '花痴', '208': '小样儿', '210': '飙泪',
    '211': '我不看', '212': '托腮', '214': '啵啵', '215': '糊脸', '216': '拍头', '217': '扯一扯', '218': '舔一舔', '219': '蹭一蹭',
    '220': '拽炸天', '221': '顶呱呱', '222': '抱抱', '223': '暴击', '224': '开枪', '225': '撩一撩', '226': '拍桌', '227': '拍手',
    '228': '恭喜', '229': '干杯', '230': '嘲讽', '231': '哼', '232': '佛系', '233': '掐一掐', '234': '惊呆', '235': '颤抖',
    '236': '啃头', '237': '偷看', '238': '扇脸', '239': '原谅', '240': '喷脸', '241': '生日快乐', '242': '头撞击', '243': '甩头',
    '244': '扔狗', '245': '加油必胜', '246': '加油抱抱', '247': '口罩护体', '260': '搬砖中', '261': '忙到飞起', '262': '脑阔疼', '263': '沧桑',
    '264': '捂脸', '265': '辣眼睛', '266': '哦哟', '267': '头秃', '268': '问号脸', '269': '暗中观察', '270': 'emm', '271': '吃瓜',
    '272': '呵呵哒', '273': '我酸了', '274': '太南了', '276': '辣椒酱', '277': '汪汪', '278': '汗', '279': '打脸', '280': '击掌',
    '281': '无眼笑', '282': '敬礼', '283': '狂笑', '284': '面无表情', '285': '摸鱼', '286': '魔鬼笑', '287': '哦', '288': '请',
    '289': '睁眼', '290': '敲开心', '291': '震惊', '292': '让我康康', '293': '摸锦鲤', '294': '期待', '295': '拿到红包', '296': '真好',
    '297': '拜谢', '298': '元宝', '299': '牛啊', '300': '胖三斤', '301': '好闪', '302': '左拜年', '303': '右拜年', '304': '红包包',
    '305': '右亲亲', '306': '牛气冲天', '307': '喵喵', '308': '求红包', '309': '谢红包', '310': '新年烟花', '311': '打call', '312': '变形',
    '313': '嗑到了', '314': '仔细分析', '315': '加油', '316': '我没事', '317': '菜狗', '318': '崇拜', '319': '比心', '320': '庆祝',
    '321': '老色痞', '322': '拒绝', '323': '嫌弃', '324': '吃糖', '325': '惊吓', '326': '生气', '327': '加一', '328': '错号',
    '329': '对号', '330': '完成', '331': '明白', '332': '举牌牌', '333': '烟花', '334': '虎虎生威', '336': '豹富', '337': '花朵脸',
    '338': '我想开了', '339': '舔屏', '340': '热化了', '341': '打招呼', '342': '酸Q', '343': '我方了', '344': '大怨种', '345': '红包多多',
    '346': '你真棒棒', '347': '大展宏兔', '348': '福萝卜',
}

# ---------------------------------------------------------------------------
# 348 之后的具名系统表情（349–431，79 条，逐条照抄上游）
# ---------------------------------------------------------------------------
QQ_NATIVE_FACE_NAME_EXTENSIONS = {
    '349': '坚强', '350': '贴贴', '351': '敲敲', '352': '咦', '353': '拜托', '354': '尊嘟假嘟', '355': '耶', '356': '666',
    '357': '裂开', '358': '骰子', '359': '包剪锤', '360': '亲亲', '361': '狗狗笑哭', '362': '好兄弟', '363': '狗狗可怜', '364': '超级赞',
    '365': '狗狗生气', '366': '芒狗', '367': '狗狗疑问', '368': '奥特笑哭', '369': '彩虹', '370': '祝贺', '371': '冒泡', '372': '气呼呼',
    '373': '忙', '374': '波波流泪', '375': '超级鼓掌', '376': '跺脚', '377': '嗨', '378': '企鹅笑哭', '379': '企鹅流泪', '380': '真棒',
    '381': '路过', '382': 'emo', '383': '企鹅爱心', '384': '晚安', '385': '太气了', '386': '呜呜呜', '387': '太好笑', '388': '太头疼',
    '389': '太赞了', '390': '太头秃', '391': '太沧桑', '392': '龙年快乐', '393': '新年中龙', '394': '新年大龙', '395': '略略略', '396': '狼狗',
    '397': '抛媚眼', '398': '超级ok', '399': 'tui', '400': '快乐', '401': '超级转圈', '402': '别说话', '403': '出去玩', '404': '闪亮登场',
    '405': '好运来', '406': '姐是女王', '407': '我听听', '408': '臭美', '409': '送你花花', '410': '么么哒', '411': '一起嗨', '412': '开心',
    '413': '摇起来', '415': '划龙舟', '416': '中龙舟', '417': '大龙舟', '419': '火车', '420': '中火车', '421': '大火车', '424': '续标识',
    '425': '求放过', '426': '玩火', '427': '偷感', '428': '收到', '429': '蛇年快乐', '430': '蛇身', '431': '蛇尾',
}

# 表情 ID → 名称（扩展表优先，与上游 qqNativeFaceName 的查表顺序一致）
QQ_NATIVE_FACE_NAMES = {**QFACE_SYSTEM_FACE_NAMES, **QQ_NATIVE_FACE_NAME_EXTENSIONS}

# 反向索引：名称 → 表情 ID（如 '微笑' → '14'）。等价 qface.getByText 的精确名称匹配，
# 不做拼音/别名模糊匹配，避免臆测。注意 '拜托' 在系统表(200)与扩展表(353)各有一条，
# 合并时扩展表优先，故反向索引取 353（354 个 ID / 353 个不同名称）。
QQ_NATIVE_FACE_IDS_BY_NAME = {name: key for key, name in QQ_NATIVE_FACE_NAMES.items()}

# 上游正则：(?:^|[\s,])key\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))，带 'i' 标志
_ATTRIBUTE_VALUE_PATTERN = r"""(?:^|[\s,]){key}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""

# 上游正则：/<face\b([^>]*)>(?:<\/face>)?/gi
_FACE_TAG_PATTERN = re.compile(r"<face\b([^>]*)>(?:</face>)?", re.IGNORECASE)
# 上游正则：/\[CQ:face,([^\]]*)\]/gi
_CQ_FACE_PATTERN = re.compile(r"\[CQ:face,([^\]]*)\]", re.IGNORECASE)
# 上游正则：/<mface\b([^>]*)>(?:<\/mface>)?/gi
_MFACE_TAG_PATTERN = re.compile(r"<mface\b([^>]*)>(?:</mface>)?", re.IGNORECASE)


def _stringify(value):
    '''等价 JS ``String(value ?? '')`` 的防御性读取。

    - ``None``（对应 null/undefined）→ ``''``（``?? ''`` 的部分）
    - ``str`` 原样返回（**不** trim；上游的 trim 由调用方在取值后做）
    - ``bool`` → ``'true'`` / ``'false'``（对齐 JS ``String(true)``）
    - ``int`` → 十进制文本；``float`` 为整数值时去掉 ``.0``（对齐 JS ``String(14.0) === '14'``），
      非有限值对齐 JS 输出 ``'NaN'`` / ``'Infinity'`` / ``'-Infinity'``
    - ``list``/``tuple`` → 逐项递归后以 ``,`` 连接（对齐 JS ``Array.prototype.toString``）
    - 其它对象 → ``'[object Object]'``（对齐 JS ``String({})``；见下方"已知差异"）

    已知差异（上游入参只可能是 string/number，以下分支不会在真实链路触发）：
    自定义 ``toString`` 的对象：JS 调其 ``toString``，Python 统一走 ``'[object Object]'``
    （仅当类自己重写 ``__str__`` 时才等价）。
    '''
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, (list, tuple)):
        return ','.join(_stringify(item) for item in value)
    return '[object Object]'


def _attribute_value(attributes, key):
    '''从段标记的属性文本里读取 ``key`` 的值（等价上游 attributeValue）。

    上游：
        const match = new RegExp(``(?:^|[\\s,])${key}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))``, 'i').exec(attributes)
        return (match?.[1] ?? match?.[2] ?? match?.[3] ?? '').trim()

    JS 的 ``??`` 仅在 null/undefined 时回落；正则捕获组未参与匹配时 JS 为 ``undefined``，
    Python 为 ``None``，故此处 ``or ''`` 与上游语义一致。缺失/非法属性文本返回 ``''``。
    '''
    pattern = re.compile(_ATTRIBUTE_VALUE_PATTERN.replace('{key}', re.escape(key)), re.IGNORECASE)
    match = pattern.search(_stringify(attributes))
    if not match:
        return ''
    return (match.group(1) or match.group(2) or match.group(3) or '').strip()


def qq_native_face_name(face_id):
    '''ID → 表情名称；未收录返回 ``None``（对应上游返回 undefined）。

    上游：
        const key = String(id ?? '').trim()
        if (!key) return undefined
        const extension = QQ_NATIVE_FACE_NAME_EXTENSIONS[key]
        if (extension) return extension
        const face = qface.get(key)
        return face?.QDes?.replace(/^\\//, '').trim() || undefined
    '''
    key = _stringify(face_id).strip()
    if not key:
        return None
    extension = QQ_NATIVE_FACE_NAME_EXTENSIONS.get(key)
    if extension:
        return extension
    name = QFACE_SYSTEM_FACE_NAMES.get(key)
    # 内联表在生成时已完成 replace(/^\\//, '').trim()；空名称按上游 `|| undefined` 归为 None
    return name if name else None


def describe_qq_native_face(face_id):
    '''把 ID 描述为叙述者可见的稳定文字（等价上游 describeQQNativeFace）。'''
    key = _stringify(face_id).strip()
    if not key:
        return '[QQ 原生表情（未提供 ID）]'
    name = qq_native_face_name(key)
    if name:
        return f'[QQ 原生表情：{name}（ID: {key}）]'
    return f'[QQ 原生表情（ID: {key}；名称未收录）]'


def normalize_qq_native_face_segments(content):
    '''把适配器标记转换为稳定的叙述者可见语义，不让模型去猜图标。

    等价上游 normalizeQQNativeFaceSegments，按顺序做三次替换：
    1. ``<face id="...">`` / ``<face id="..."></face>``（大小写不敏感）→ 原生表情描述
    2. ``[CQ:face,id=...]``（大小写不敏感）→ 原生表情描述
    3. ``<mface summary="...">``（summary 缺失时回落 name）→ ``[QQ 商城表情：X]`` / ``[QQ 商城表情]``
    '''
    text = _stringify(content)

    def face_replacement(match):
        return describe_qq_native_face(_attribute_value(match.group(1), 'id'))

    def mface_replacement(match):
        attributes = match.group(1)
        name = _attribute_value(attributes, 'summary') or _attribute_value(attributes, 'name')
        return f'[QQ 商城表情：{name}]' if name else '[QQ 商城表情]'

    text = _FACE_TAG_PATTERN.sub(face_replacement, text)
    text = _CQ_FACE_PATTERN.sub(face_replacement, text)
    return _MFACE_TAG_PATTERN.sub(mface_replacement, text)


__all__ = [
    'QFACE_SYSTEM_FACE_NAMES',
    'QQ_NATIVE_FACE_NAMES',
    'QQ_NATIVE_FACE_NAME_EXTENSIONS',
    'QQ_NATIVE_FACE_IDS_BY_NAME',
    'qq_native_face_name',
    'describe_qq_native_face',
    'normalize_qq_native_face_segments',
]
