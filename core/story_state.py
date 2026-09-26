"""上游 `src/story-state.ts` 的对应物：版本化故事状态编解码与幂等升级。

约定（见键名约定）：
- Python 侧输出一律 snake_case；读入时同时接受上游 camelCase（旧数据/模型产物）。
- 未知的顶层键会被搬进 `extensions` 保留，而不是在写回时丢失。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .agency import normalize_agency_window_state
from .alter import normalize_alter_system_state

__all__ = [
    "CURRENT_STORY_STATE_VERSION",
    "StoryStateMigrationInspection",
    "inspect_story_state_migration",
    "decode_story_state",
    "upgrade_story_state",
    "normalize_scene_frame",
    "normalize_dialogue_burst",
    "encode_story_state",
    "normalize_automatic_delivery_summaries",
    "normalize_scene_presence_state",
    "normalize_working_details",
    "normalize_timeline_carry",
    "normalize_continuity_snapshot",
]

# Version 1 是第一个「读写集中在一处」的状态信封；更早的 beta10 行没有版本号，
# 读取时升级。
CURRENT_STORY_STATE_VERSION = 4

# 已知顶层键的**两种拼写**（snake_case 输出 / 上游 camelCase 输入）。
# 不在这两个集合里的键会被搬进 extensions。
_KNOWN_SNAKE_KEYS = frozenset({
    "schema_version", "extensions", "setting_overlay", "active_scene_id", "active_arc_id",
    "continuity_snapshot", "narrative_update_count", "last_continuity_update_at",
    "continuity_dirty", "automation", "alter_system", "agency_window", "scene_presence",
    "automatic_delivery_summaries", "working_details", "timeline_carry", "chat_rhythm",
    "scene_frame", "dialogue_burst", "working_detail_resolutions",
})

# 上游 camelCase 拼写 → snake_case，用于识别「已知键」。
_CAMEL_TO_SNAKE = {
    "schemaVersion": "schema_version",
    "settingOverlay": "setting_overlay",
    "activeSceneId": "active_scene_id",
    "activeArcId": "active_arc_id",
    "continuitySnapshot": "continuity_snapshot",
    "narrativeUpdateCount": "narrative_update_count",
    "lastContinuityUpdateAt": "last_continuity_update_at",
    "continuityDirty": "continuity_dirty",
    "alterSystem": "alter_system",
    "agencyWindow": "agency_window",
    "scenePresence": "scene_presence",
    "automaticDeliverySummaries": "automatic_delivery_summaries",
    "workingDetails": "working_details",
    "workingDetailResolutions": "working_detail_resolutions",
    "timelineCarry": "timeline_carry",
    "chatRhythm": "chat_rhythm",
    "sceneFrame": "scene_frame",
    "dialogueBurst": "dialogue_burst",
}

_KNOWN_ALL_KEYS = _KNOWN_SNAKE_KEYS | frozenset(_CAMEL_TO_SNAKE)

_SCENE_FRAME_FIELDS = (
    "place", "present_people", "ongoing_activity", "attention",
    "device_access", "privacy", "open_motions", "open_topics",
)

# 上游用这些 camelCase 名读 sources 里的子字段。
_SCENE_FRAME_CAMEL = {
    "place": "place",
    "present_people": "presentPeople",
    "ongoing_activity": "ongoingActivity",
    "attention": "attention",
    "device_access": "deviceAccess",
    "privacy": "privacy",
    "open_motions": "openMotions",
    "open_topics": "openTopics",
}

_EPOCH_ISO = datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


class StoryStateMigrationInspection(dict):
    """`inspect_story_state_migration` 的返回结构（TypedDict 语义，运行期是 dict）。"""


# --------------------------------------------------------------------------- #
# 基础助手
# --------------------------------------------------------------------------- #

def _is_record(value: Any) -> bool:
    return bool(value) and isinstance(value, dict)


def _pick(value: Any, camel: str, snake: str | None = None) -> Any:
    """从外部读入时两种拼写都认，优先上游 camelCase。"""
    if not isinstance(value, dict):
        return None
    if camel in value:
        return value[camel]
    return value.get(snake) if snake else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _clipped_text_or_none(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _valid_iso(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _finite_number(value: Any) -> float | None:
    # bool 在 Python 里是 int 的子类，上游 Boolean 不算 number。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _finite_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _integer_array(value: Any, limit: int) -> list[int]:
    if not isinstance(value, list):
        return []
    out = [item for item in value if isinstance(item, int) and not isinstance(item, bool)]
    return out[:limit]


def _string_array(value: Any, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:item_limit]
        if text and text not in seen:
            seen.append(text)
    return seen[:limit]


# --------------------------------------------------------------------------- #
# 迁移检查
# --------------------------------------------------------------------------- #

def inspect_story_state_migration(value: Any, configured_perspective: str = "") -> dict[str, Any]:
    record = value if _is_record(value) else {}
    schema_version = _finite_integer(_pick(record, "schemaVersion", "schema_version"))
    return {
        "source_version": schema_version if schema_version is not None else 0,
        "target_version": CURRENT_STORY_STATE_VERSION,
        "upgraded": (schema_version if schema_version is not None else 0) != CURRENT_STORY_STATE_VERSION,
        "unknown_keys": sorted(
            key for key in record if key not in _KNOWN_ALL_KEYS
        ),
        "perspective_default_available": bool(configured_perspective and configured_perspective.strip()),
    }


def decode_story_state(value: Any) -> dict[str, Any]:
    """把任意已持久化版本解码为当前完整状态信封。"""
    return upgrade_story_state(value)


def upgrade_story_state(value: Any) -> dict[str, Any]:
    """纯函数且幂等。未知顶层键被搬进 extensions，供后续版本恢复。"""
    record = value if _is_record(value) else {}
    overlay = _pick(record, "settingOverlay", "setting_overlay")
    overlay = overlay if isinstance(overlay, dict) else {}
    automation = record.get("automation")
    automation = automation if isinstance(automation, dict) else {}
    existing_extensions = record.get("extensions")
    existing_extensions = existing_extensions if isinstance(existing_extensions, dict) else {}
    unknown_extensions = {
        key: item for key, item in record.items() if key not in _KNOWN_ALL_KEYS
    }
    extensions = {**existing_extensions, **unknown_extensions}
    continuity = normalize_continuity_snapshot(
        _pick(record, "continuitySnapshot", "continuity_snapshot")
    )

    raw_resolutions = _pick(record, "workingDetailResolutions", "working_detail_resolutions")
    raw_resolutions = raw_resolutions if isinstance(raw_resolutions, dict) else {}
    resolutions = {
        label: int(item)
        for label, item in list(
            (label, item) for label, item in raw_resolutions.items()
            if len(label) <= 80 and isinstance(item, int) and not isinstance(item, bool) and item > 0
        )[-32:]
    }

    raw_traits = overlay.get("characterTraits", overlay.get("character_traits"))
    raw_follow_ups = automation.get(
        "conversationFollowUpAt", automation.get("conversation_follow_up_at")
    )

    state: dict[str, Any] = {
        "schema_version": CURRENT_STORY_STATE_VERSION,
        "setting_overlay": {
            "character_profile": _text_or_none(
                overlay.get("characterProfile", overlay.get("character_profile"))
            ),
            "perspective": _clipped_text_or_none(overlay.get("perspective"), 1000),
            "relationship": _text_or_none(overlay.get("relationship")),
            "world": _text_or_none(overlay.get("world")),
            "supporting_cast": _text_or_none(
                overlay.get("supportingCast", overlay.get("supporting_cast"))
            ),
            "location": _text_or_none(overlay.get("location")),
            "character_traits": [
                item for item in raw_traits if isinstance(item, str)
            ] if isinstance(raw_traits, list) else [],
        },
        "active_scene_id": _finite_number(_pick(record, "activeSceneId", "active_scene_id")),
        "active_arc_id": _finite_number(_pick(record, "activeArcId", "active_arc_id")),
        "continuity_snapshot": continuity,
        "narrative_update_count": max(
            0, int(_finite_number(_pick(record, "narrativeUpdateCount", "narrative_update_count")) or 0)
        ),
        "last_continuity_update_at": _text_or_none(
            _pick(record, "lastContinuityUpdateAt", "last_continuity_update_at")
        ),
        "continuity_dirty": _pick(record, "continuityDirty", "continuity_dirty") is True,
        "alter_system": normalize_alter_system_state(
            _pick(record, "alterSystem", "alter_system")
        ),
        "agency_window": normalize_agency_window_state(
            _pick(record, "agencyWindow", "agency_window")
        ),
        "scene_presence": normalize_scene_presence_state(
            _pick(record, "scenePresence", "scene_presence")
        ),
        "working_details": normalize_working_details(
            _pick(record, "workingDetails", "working_details")
        ),
        "working_detail_resolutions": resolutions,
        "timeline_carry": normalize_timeline_carry(
            _pick(record, "timelineCarry", "timeline_carry")
        ),
        "automatic_delivery_summaries": normalize_automatic_delivery_summaries(
            _pick(record, "automaticDeliverySummaries", "automatic_delivery_summaries")
        ),
        # beta10 声明了这个字段，但它的旧解码器忘了返回它。
        "chat_rhythm": (
            _pick(record, "chatRhythm", "chat_rhythm")
            if _is_record(_pick(record, "chatRhythm", "chat_rhythm"))
            else None
        ),
        "scene_frame": normalize_scene_frame(_pick(record, "sceneFrame", "scene_frame")),
        "dialogue_burst": normalize_dialogue_burst(
            _pick(record, "dialogueBurst", "dialogue_burst")
        ),
        "automation": {
            "quiet_until": _text_or_none(automation.get("quietUntil", automation.get("quiet_until"))),
            "next_advance_at": _text_or_none(
                automation.get("nextAdvanceAt", automation.get("next_advance_at"))
            ),
            "timeline_retry_at": _text_or_none(
                automation.get("timelineRetryAt", automation.get("timeline_retry_at"))
            ),
            "timeline_retry_from": _text_or_none(
                automation.get("timelineRetryFrom", automation.get("timeline_retry_from"))
            ),
            "last_auto_advance_at": _text_or_none(
                automation.get("lastAutoAdvanceAt", automation.get("last_auto_advance_at"))
            ),
            "last_user_message_at": _text_or_none(
                automation.get("lastUserMessageAt", automation.get("last_user_message_at"))
            ),
            "conversation_follow_up_at": [
                item for item in raw_follow_ups if isinstance(item, str)
            ][:8] if isinstance(raw_follow_ups, list) else [],
            "conversation_follow_up_participant_id": _clipped_text_or_none(
                automation.get(
                    "conversationFollowUpParticipantId",
                    automation.get("conversation_follow_up_participant_id"),
                ),
                255,
            ),
        },
    }
    if extensions:
        state["extensions"] = extensions
    return state


def normalize_scene_frame(value: Any) -> dict[str, Any] | None:
    if not _is_record(value) or not isinstance(value.get("id"), str) or not value["id"].strip():
        return None
    raw_sources = value.get("sources")
    raw_sources = raw_sources if isinstance(raw_sources, dict) else {}
    sources: dict[str, list[int]] = {}
    for field in _SCENE_FRAME_FIELDS:
        ids = _integer_array(
            raw_sources.get(field, raw_sources.get(_SCENE_FRAME_CAMEL[field])), 24
        )
        if ids:
            sources[field] = ids
    # 版本 3 有意丢弃 beta1 那种 prose 形状的来源字段。保留它们的 id 会让
    # 一个已被清理的 frame 看起来仍被过期 prose 支撑。
    source_entry_ids: list[int] = []
    for ids in sources.values():
        for item in ids or []:
            if item not in source_entry_ids:
                source_entry_ids.append(item)
    source_entry_ids = source_entry_ids[:80]

    def grounded_text(field: str, raw: Any, limit: int) -> str | None:
        return _clipped_text_or_none(raw, limit) if sources.get(field) else None

    def grounded_list(field: str, raw: Any, count: int, limit: int) -> list[str]:
        return _string_array(raw, count, limit) if sources.get(field) else []

    local_boundary = _finite_integer(
        value.get("localBoundaryEntryId", value.get("local_boundary_entry_id"))
    )
    scene_id = _finite_integer(value.get("sceneId", value.get("scene_id")))
    frame: dict[str, Any] = {"id": value["id"].strip()[:120]}
    if local_boundary:
        frame["local_boundary_entry_id"] = local_boundary
    if scene_id:
        frame["scene_id"] = scene_id
    frame.update({
        "place": grounded_text("place", value.get("place"), 240),
        "present_people": grounded_list("present_people", value.get("presentPeople", value.get("present_people")), 16, 80),
        "ongoing_activity": grounded_text("ongoing_activity", value.get("ongoingActivity", value.get("ongoing_activity")), 320),
        "attention": grounded_text("attention", value.get("attention"), 320),
        "device_access": grounded_text("device_access", value.get("deviceAccess", value.get("device_access")), 80),
        "privacy": grounded_text("privacy", value.get("privacy"), 80),
        "open_motions": grounded_list("open_motions", value.get("openMotions", value.get("open_motions")), 8, 320),
        "open_topics": grounded_list("open_topics", value.get("openTopics", value.get("open_topics")), 8, 320),
        "source_entry_ids": source_entry_ids,
        "sources": sources,
        "updated_at": _valid_iso(value.get("updatedAt", value.get("updated_at"))) or _EPOCH_ISO,
    })
    return frame


def normalize_dialogue_burst(value: Any) -> dict[str, Any] | None:
    if not _is_record(value):
        return None
    raw_id, raw_frame = value.get("id"), value.get("frameId", value.get("frame_id"))
    if not isinstance(raw_id, str) or not isinstance(raw_frame, str):
        return None
    burst_id = raw_id.strip()[:160]
    frame_id = raw_frame.strip()[:120]
    started_at = _valid_iso(value.get("startedAt", value.get("started_at")))
    if not burst_id or not frame_id or not started_at:
        return None
    return {
        "id": burst_id,
        "frame_id": frame_id,
        "started_at": started_at,
        "source_entry_ids": _integer_array(
            value.get("sourceEntryIds", value.get("source_entry_ids")), 80
        ),
        "last_event_id": _clipped_text_or_none(
            value.get("lastEventId", value.get("last_event_id")), 180
        ),
        "scope_key": _clipped_text_or_none(
            value.get("scopeKey", value.get("scope_key")), 120
        ),
        "topic_keys": _string_array(value.get("topicKeys", value.get("topic_keys")), 12, 120),
    }


def encode_story_state(value: Any) -> dict[str, Any]:
    """走同一个编解码器，写入永远不能绕过归一化。"""
    return upgrade_story_state(value)


def normalize_automatic_delivery_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not _is_record(item):
            continue
        participant_id = _clipped_text_or_none(
            item.get("participantId", item.get("participant_id")), 255
        ) or ""
        summary = _clipped_text_or_none(item.get("summary"), 240) or ""
        delivered_at = _valid_iso(item.get("deliveredAt", item.get("delivered_at"))) or ""
        source_entry_id = _finite_integer(
            item.get("sourceEntryId", item.get("source_entry_id"))
        )
        key = f"{participant_id}|{source_entry_id or 0}|{summary}"
        if not participant_id or not summary or not delivered_at or key in seen:
            continue
        seen.add(key)
        entry: dict[str, Any] = {
            "participant_id": participant_id,
            "summary": summary,
        }
        if source_entry_id:
            entry["source_entry_id"] = source_entry_id
        entry["delivered_at"] = delivered_at
        normalized.append(entry)
    return normalized[-6:]


def normalize_scene_presence_state(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    latest: dict[str, dict[str, Any]] = {}
    for item in value:
        if not _is_record(item):
            continue
        name = _clipped_text_or_none(item.get("name"), 80) or ""
        status = item.get("status")
        if status not in ("present", "off-scene", "expected"):
            status = None
        basis = _clipped_text_or_none(item.get("basis"), 300) or ""
        source_entry_ids = _integer_array(
            item.get("sourceEntryIds", item.get("source_entry_ids")), 8
        )
        updated_at = _valid_iso(item.get("updatedAt", item.get("updated_at"))) or ""
        if not name or not status or not basis or not source_entry_ids or not updated_at:
            continue
        latest[name] = {
            "name": name,
            "status": status,
            "basis": basis,
            "source_entry_ids": source_entry_ids,
            "updated_at": updated_at,
        }
    return list(latest.values())[-8:]


def normalize_working_details(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    latest: dict[str, dict[str, Any]] = {}
    for item in value:
        if not _is_record(item):
            continue
        label = _clipped_text_or_none(item.get("label"), 80) or ""
        detail_value = _clipped_text_or_none(item.get("value"), 300) or ""
        if not label or not detail_value:
            continue
        expires_at = _valid_iso(item.get("expiresAt", item.get("expires_at")))
        created_at = (
            _valid_iso(item.get("createdAt", item.get("created_at"))) or _EPOCH_ISO
        )
        source_entry_ids = _integer_array(
            item.get("sourceEntryIds", item.get("source_entry_ids")), 8
        )
        detail: dict[str, Any] = {"label": label}
        raw_participant = item.get("participantId", item.get("participant_id"))
        if isinstance(raw_participant, str):
            detail["participant_id"] = raw_participant[:255]
        detail["value"] = detail_value
        if expires_at:
            detail["expires_at"] = expires_at
        detail["created_at"] = created_at
        if source_entry_ids:
            detail["source_entry_ids"] = source_entry_ids
        knowledge = item.get("knowledge")
        if _is_record(knowledge):
            detail["knowledge"] = knowledge
        latest[label] = detail
    return list(latest.values())[-10:]


def normalize_timeline_carry(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:240]
        if text and text not in seen:
            seen.append(text)
    return seen[:4]


def normalize_continuity_snapshot(value: Any) -> dict[str, Any] | None:
    if not _is_record(value):
        return None

    def text(item: Any, limit: int) -> str:
        return item.strip()[:limit] if isinstance(item, str) else ""

    def items(item: Any, limit: int) -> list[str]:
        if not isinstance(item, list):
            return []
        return [t for t in (text(entry, limit) for entry in item) if t][:5]

    current = text(value.get("current"), 500)
    recent = items(value.get("recent"), 300)
    salient = items(value.get("salient"), 400)
    if not current and not recent and not salient:
        return None
    return {"current": current, "next": [], "recent": recent, "salient": salient}
