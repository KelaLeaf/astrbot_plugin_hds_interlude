"""上游 `src/script/scene-frame.ts` 的 Python 对应物：M4 物化视图投影。

把**已经确立的证据**确定性地投影成一份只读的场景脚手架。它从不请求模型去总结场景，
也从不因为「时间流逝」而改变 frame 或 burst 的身份。

移植自 Koishi / TypeScript 上游快照 `upstream/src/script/scene-frame.ts`（222 行）。

关键对应：
- `projectSceneFrame` → `project_scene_frame`
- `resolveDialogueBurst` → `resolve_dialogue_burst`
- `advanceSceneFrame` → `advance_scene_frame`
- `sceneFrameProvenanceErrors` → `scene_frame_provenance_errors`
- 入参接口 `SceneFrameProjectionInput` / `DialogueBurstSignal` 同名保留。
- 私有辅助 `assign` / `applyLifeHandoff` / `cloneFrame` / `positiveIds` / `unionIds` /
  `dialogueTopicKeys` / `topicKeysOverlap` / `isConversationalFollowUp` / `unionStrings` /
  `stableId` → 同名前缀 `_` 的模块级函数。

两处 Python 侧的必要适配（语义不变）：
- `frame.sources` 的键是 snake_case 字段名（`posture_or_motion` / `affective_baseline` /
  `narrative_focus`），`cloneFrame` 的过滤名单随之改写。
- JS 正则 `[\\p{P}\\p{S}\\s]` 与 `[\\p{L}\\p{N}]{3,}` 依赖 Unicode Property Escapes，
  Python 标准库 `re` 不支持，故改用 `unicodedata.category()` 逐码点分类，判别结果一致。

`life-handoff` 由并行的 `core/script/life_handoff.py` 移植任务负责。本模块按约定接口
`entry_life_handoff(entry)` 导入；若该文件尚未落地，则降级为「没有交接」而不是自造实现。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Required, TypedDict

from ..time import iso
from ..types import (
    AgencyWindowState,
    DialogueBurstState,
    InterludeScene,
    SceneFrame,
    ScenePresenceState,
    ScriptEntry,
    StoryState,
    WorkingDetail,
)
from .contract import ScriptCommitDraft

if TYPE_CHECKING:
    from .life_handoff import LifeHandoff

try:  # 上游 `./life-handoff`；由并行的 `core/script/life_handoff.py` 移植任务落地。
    from .life_handoff import entry_life_handoff
except ImportError:  # pragma: no cover - 仅在并行模块尚未落地时生效
    def entry_life_handoff(entry: ScriptEntry) -> "LifeHandoff | None":
        """降级实现：没有交接证据 → 场景不因该条目发生结构性变化。"""
        return None


def _js_truthy(value: Any) -> bool:
    """JS 真值语义（`Array#length` / 非空字符串等）。"""
    if value is None or value is False:
        return False
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value != 0
    return True


def _coalesce(*values: Any) -> Any:
    """等价 JS `??`：只在 `undefined` / `null` 时取下一个候选。"""
    for value in values:
        if value is not None:
            return value
    return None


class SceneFrameProjectionInput(TypedDict, total=False):
    """`projectSceneFrame` 的入参（上游 `SceneFrameProjectionInput`）。"""

    story_id: Required[str]
    now: Required[datetime]
    # 活动场景查询是权威来源；可以为空（root frame）。
    scene: Required[InterludeScene | None]
    state: Required[StoryState]
    recent_entries: list[ScriptEntry]
    working_details: list[WorkingDetail]
    scene_presence: list[ScenePresenceState]
    agency_window: AgencyWindowState | None


class DialogueBurstSignal(TypedDict, total=False):
    """`resolveDialogueBurst` 的第三个入参（上游 `DialogueBurstSignal`）。"""

    # 关系/群组/生活作用域，落库前会被哈希。
    scope: str
    # 当前外部事件文本；只有哈希后的话题键会被持久化。
    topic_text: str
    # 一次已提交的结构性转折，绝不是「时间到的超时」。
    boundary: bool


def project_scene_frame(input: SceneFrameProjectionInput) -> SceneFrame:
    """把已确立的证据确定性地投影成只读场景脚手架。"""
    # 活动场景查询是权威的。持久化的 activeSceneId 可能短暂地指向一个刚被关闭的场景。
    scene = input.get('scene')
    scene_id = (scene or {}).get('id')
    frame_id = _stable_id('frame', input['story_id'], str(scene_id if scene_id is not None else 'root'))
    # 每个回合都从事实来源重新投影。持久化的 beta1 frame 可能残留散文尾巴；
    # 克隆它们会让那个反馈回路一直活下去。
    values: SceneFrame = {'id': frame_id}
    if scene_id:
        values['scene_id'] = scene_id
    values['present_people'] = []
    values['open_motions'] = []
    values['open_topics'] = []
    previous_frame = (input.get('state') or {}).get('scene_frame') or {}
    if _js_truthy(previous_frame.get('local_boundary_entry_id')):
        values['local_boundary_entry_id'] = previous_frame['local_boundary_entry_id']
    values['source_entry_ids'] = []
    values['sources'] = {}
    values['updated_at'] = iso(input['now'])

    presence: list[ScenePresenceState] = _coalesce(
        input.get('scene_presence'),
        (input.get('state') or {}).get('scene_presence'),
        [],
    )
    recent_entries = input.get('recent_entries')
    visible = {entry['id'] for entry in recent_entries} if recent_entries is not None else None

    def grounded(ids: list[int]) -> bool:
        return len(ids) > 0 and (visible is None or any(entry_id in visible for entry_id in ids))

    present = [
        item for item in presence
        if item.get('status') == 'present'
        and grounded(item.get('source_entry_ids') or [])
        and max(item['source_entry_ids']) >= (values.get('local_boundary_entry_id') or 0)
    ]
    if present:
        _assign(
            values, 'present_people', [item['name'] for item in present],
            [entry_id for item in present for entry_id in item.get('source_entry_ids') or []],
        )

    details: list[WorkingDetail] = _coalesce(
        input.get('working_details'),
        (input.get('state') or {}).get('working_details'),
        [],
    )
    grounded_details = [item for item in details if grounded(item.get('source_entry_ids') or [])]
    if grounded_details:
        _assign(
            values, 'open_motions',
            [f"{item['label']}：{item['value']}" for item in grounded_details],
            [entry_id for item in grounded_details for entry_id in item.get('source_entry_ids') or []],
        )

    agency: AgencyWindowState | None = _coalesce(
        input.get('agency_window'),
        (input.get('state') or {}).get('agency_window'),
    )
    if agency and len(agency.get('source_entry_ids') or []):
        _assign(values, 'device_access', agency['device_access'], agency['source_entry_ids'])
        _assign(values, 'privacy', agency['privacy'], agency['source_entry_ids'])
        _assign(values, 'attention', agency['activity_load'], agency['source_entry_ids'])

    # 每条交接都指回原文。新的地点/结构转折替换的是本地占用，
    # 而不是长期关系或剧情弧。
    for entry in sorted(input.get('recent_entries') or [], key=lambda item: item['id']):
        handoff = entry_life_handoff(entry)
        if not handoff:
            continue
        _apply_life_handoff(values, handoff, entry['id'])

    values['source_entry_ids'] = _union_ids(
        entry_id for ids in values['sources'].values() for entry_id in (ids or [])
    )
    values['updated_at'] = iso(input['now'])
    return values


def resolve_dialogue_burst(
    frame: SceneFrame,
    previous: DialogueBurstState | None,
    started_at: datetime,
    signal: DialogueBurstSignal | None = None,
) -> DialogueBurstState:
    """决定本轮对话属于哪个 burst：身份只随 frame 身份、作用域与显式边界改变，
    绝不随「闲置时长」或消息条数改变。"""
    signal = signal if signal is not None else {}
    raw_scope = (signal.get('scope') or '').strip()
    scope_key = _stable_id('scope', raw_scope) if raw_scope else None
    raw_topic = signal.get('topic_text') or ''
    next_topic_keys = _dialogue_topic_keys(raw_topic)
    previous = previous if previous is not None else {}
    previous_topic_keys = previous.get('topic_keys') or []
    same_scope = (not scope_key) or (not previous.get('scope_key')) or scope_key == previous.get('scope_key')
    if not next_topic_keys or not previous_topic_keys:
        same_topic = True
    else:
        same_topic = _topic_keys_overlap(previous_topic_keys, next_topic_keys) or _is_conversational_follow_up(raw_topic)
    if not signal.get('boundary') and previous.get('frame_id') == frame['id'] and same_scope and same_topic:
        continued: DialogueBurstState = {
            **previous,
            'source_entry_ids': _union_ids([
                *(previous.get('source_entry_ids') or []),
                *frame['source_entry_ids'],
            ]),
        }
        if scope_key:
            continued['scope_key'] = scope_key
        if next_topic_keys:
            continued['topic_keys'] = _union_strings([*previous_topic_keys, *next_topic_keys], 12)
        return continued
    started_at_iso = iso(started_at)
    fresh: DialogueBurstState = {
        'id': _stable_id('burst', frame['id'], started_at_iso),
        'frame_id': frame['id'],
        'started_at': started_at_iso,
        'source_entry_ids': list(frame['source_entry_ids']),
    }
    if scope_key:
        fresh['scope_key'] = scope_key
    if next_topic_keys:
        fresh['topic_keys'] = next_topic_keys
    return fresh


def advance_scene_frame(
    frame: SceneFrame,
    burst: DialogueBurstState,
    commit: ScriptCommitDraft,
    source_entry_id: int,
    now: datetime,
    handoff: "LifeHandoff | None" = None,
) -> dict[str, Any]:
    """只物化新提交的那段 append；此前的散文保持不可变。"""
    # 剧本散文始终是不可变的叙事证据。它绝不会被投影回 frame 字段，
    # 即便是全局自动生活提交也一样。
    next_frame = _clone_frame(frame, now)
    if handoff:
        _apply_life_handoff(next_frame, handoff, source_entry_id)
    next_frame['source_entry_ids'] = _union_ids(
        entry_id for ids in next_frame['sources'].values() for entry_id in (ids or [])
    )
    last_event = commit['events'][-1] if commit['events'] else None
    next_burst: DialogueBurstState = {
        **burst,
        'source_entry_ids': _union_ids([*burst['source_entry_ids'], source_entry_id]),
        'last_event_id': last_event['event_id'] if last_event else None,
    }
    return {'frame': next_frame, 'burst': next_burst}


# `SceneFrame` 字段的 (Python 键名, 上游字段名) 对照表。诊断信息沿用**上游字面量**，
# 便于和上游日志逐字比对。
_SCENE_FRAME_FIELDS: tuple[tuple[str, str], ...] = (
    ('place', 'place'),
    ('present_people', 'presentPeople'),
    ('ongoing_activity', 'ongoingActivity'),
    ('posture_or_motion', 'postureOrMotion'),
    ('attention', 'attention'),
    ('device_access', 'deviceAccess'),
    ('privacy', 'privacy'),
    ('affective_baseline', 'affectiveBaseline'),
    ('open_motions', 'openMotions'),
    ('open_topics', 'openTopics'),
    ('narrative_focus', 'narrativeFocus'),
)


def scene_frame_provenance_errors(frame: SceneFrame) -> list[str]:
    """每个被填充的字段都必须带来源条目；返回缺失来源的诊断信息。"""
    errors: list[str] = []
    sources = frame.get('sources') or {}
    for field, upstream_field in _SCENE_FRAME_FIELDS:
        value = frame.get(field)
        populated = len(value) > 0 if isinstance(value, list) else _js_truthy(value.strip() if isinstance(value, str) else value)
        if populated and not (sources.get(field) or []):
            errors.append(f'{upstream_field} has no source entry')
    return errors


def _assign(frame: SceneFrame, field: str, value: Any, source_ids: Iterable[int | None]) -> None:
    """上游私有 `assign`：只有拿到正数来源 id 时才写入字段并记录溯源。"""
    ids = _positive_ids(source_ids)
    if not ids:
        return
    frame[field] = value
    frame['sources'][field] = ids


def _apply_life_handoff(values: SceneFrame, handoff: "LifeHandoff", entry_id: int) -> None:
    """把一条生活交接施加到 frame 上；结构性转折会清空本地占用。"""
    if entry_id < (values.get('local_boundary_entry_id') or 0):
        return
    place = handoff.get('place')
    if handoff.get('transition') or (place and values.get('place') != place['value']):
        values['local_boundary_entry_id'] = entry_id
        values['present_people'] = []
        values['sources'].pop('present_people', None)
        values.pop('ongoing_activity', None)
        values['sources'].pop('ongoing_activity', None)
        values['open_motions'] = []
        values['sources'].pop('open_motions', None)
    if place:
        _assign(values, 'place', place['value'], [entry_id])
    activity = handoff.get('activity')
    if activity:
        _assign(values, 'ongoing_activity', activity['value'], [entry_id])
    presence = handoff.get('presence')
    if presence:
        _assign(values, 'present_people', presence['names'], [entry_id])
    resolved_details = handoff.get('resolved_details')
    if resolved_details:
        labels = [f"{item['label']}：" for item in resolved_details]
        values['open_motions'] = [
            item for item in values['open_motions']
            if not any(item.startswith(label) for label in labels)
        ]
        if not values['open_motions']:
            values['sources'].pop('open_motions', None)


def _clone_frame(frame: SceneFrame, now: datetime) -> SceneFrame:
    """克隆 frame；`postureOrMotion` / `affectiveBaseline` / `narrativeFocus`
    是**不跨提交**的瞬时投影，克隆时连同溯源一起丢弃。"""
    clone: SceneFrame = {'id': frame['id']}
    for field in ('scene_id', 'local_boundary_entry_id', 'place', 'ongoing_activity',
                  'attention', 'device_access', 'privacy'):
        if _js_truthy(frame.get(field)):
            clone[field] = frame[field]
    clone['present_people'] = list(frame['present_people'])
    clone['open_motions'] = list(frame['open_motions'])
    clone['open_topics'] = list(frame['open_topics'])
    clone['source_entry_ids'] = list(frame['source_entry_ids'])
    clone['sources'] = {
        key: list(ids or [])
        for key, ids in frame['sources'].items()
        if key not in ('posture_or_motion', 'affective_baseline', 'narrative_focus')
    }
    clone['updated_at'] = iso(now)
    return clone


def _positive_ids(values: Iterable[int | None]) -> list[int]:
    """只保留安全正整数 id，再去重排序截断。"""
    return _union_ids(
        value for value in values
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 9_007_199_254_740_991
    )


def _union_ids(values: Iterable[int]) -> list[int]:
    """去重、升序、只保留最后 80 条（`Array.from(new Set(v)).sort().slice(-80)`）。"""
    return sorted(set(values))[-80:]


def _dialogue_topic_keys(text: str) -> list[str]:
    """把话题文本折成有界、可哈希的「话题键」；中文取相邻二字，西文取 ≥3 的词元。"""
    normalized = _strip_punctuation_symbols_space(text.lower())
    if not normalized:
        return []
    if re.search(r'[\u3400-\u9fff]', normalized):
        raw = [normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))]
    else:
        raw = _word_runs(text.lower(), 3)
    return _union_strings([_stable_id('topic', value) for value in raw], 12)


def _strip_punctuation_symbols_space(text: str) -> str:
    """等价 JS `/[\\p{P}\\p{S}\\s]+/gu` 的删除：标点、符号、空白（含 BOM）。"""
    return ''.join(
        char for char in text
        if char != '\ufeff' and not char.isspace() and unicodedata.category(char)[0] not in ('P', 'S', 'Z')
    )


def _word_runs(text: str, minimum: int) -> list[str]:
    """等价 JS `/[\\p{L}\\p{N}]{3,}/gu`：连续字母/数字且长度达标的片段。"""
    runs: list[str] = []
    current: list[str] = []
    for char in text:
        if unicodedata.category(char)[0] in ('L', 'N'):
            current.append(char)
        else:
            if len(current) >= minimum:
                runs.append(''.join(current))
            current = []
    if len(current) >= minimum:
        runs.append(''.join(current))
    return runs


def _topic_keys_overlap(left: list[str], right: list[str]) -> bool:
    known = set(left)
    return any(key in known for key in right)


def _is_conversational_follow_up(text: str) -> bool:
    """承接式开头（「那」「所以」「你」…）或空文本视为同一话题的延续。"""
    normalized = text.strip()
    if not normalized:
        return True
    return re.match(r'^(那|这|所以|然后|但是|可是|怎么|为什么|你|我|刚才|昨天|前面|不是|对啊|嗯|啊)', normalized) is not None


def _union_strings(values: Iterable[str], limit: int) -> list[str]:
    """去重（保序）后只保留最后 `limit` 条。"""
    return list(dict.fromkeys(values))[-limit:]


def _stable_id(prefix: str, *parts: str) -> str:
    """上游私有 `stableId`：`prefix:` + sha256(parts 以 \\u001f 连接) 的前 18 位十六进制。"""
    digest = hashlib.sha256('\u001f'.join(parts).encode('utf-8')).hexdigest()[:18]
    return f'{prefix}:{digest}'
