"""`plugin.core.service`：`upstream/src/service.ts` 的组装入口。

上游 `InterludeService` 有 **277 个成员**、类体 624–7084 行，单文件无法高质量
一次移植，因此按移植约定 的**受控分解**切成 10 个 mixin，
在这里用多重继承组装：

    class InterludeService(ServiceChunk0, ServiceChunk1, ..., ServiceChunk9)

**公共 API 不变**：调用方始终 `from plugin.core.service import InterludeService`，
不需要知道内部切块。缺失的 chunk 模块用 `try/except ImportError` 容忍
（并行移植期间常见），但**已存在的 chunk 一律按序混入**；最终目标是 10 个全在。

同时 re-export 全部公共符号：

- `InterludeService` / `ServiceBase` / `ServiceChunk0..9`
- `Config` 与 `config.py` 的全部公开符号（配置 TypedDict + `resolve_*` 纯函数）
- `helpers.py` 的模块级纯函数（`group_due_intents` / `normalize_database_row` /
  `extract_user_reported_times` / `detect_live_script_time_overflow` …）
- `SessionView`、`Transport` / `NullTransport`、`desktop_bridge` 入口
- 共享小工具 `pick` 与鉴别/命名辅助函数
"""

from __future__ import annotations

from typing import Any

from .base import (
    Config,
    InterludeContext,
    ServiceBase,
    ServiceChunk0,
    TimerHandle,
    is_enabled_account,
    is_one_bot_platform,
    legacy_story_id_for,
    log_fallback,
    merge_setting,
    normalize_account_id,
    normalize_database_row,
    normalize_group_id,
    participant_id_for,
    participant_id_for_story,
    pick,
    same_participant_endpoint,
    story_id_for_character,
)
from .session import SessionView
from .transport import BackgroundDelivery, NullTransport, SendResult, Transport

# --------------------------------------------------------------------------- #
# chunk 混入：按序组装，缺失的容忍（并行移植期间目录可能还不完整）
# --------------------------------------------------------------------------- #

_CHUNK_BASES: list[type] = [ServiceChunk0]
_LOADED_CHUNKS: list[str] = ['chunk0']
_MISSING_CHUNKS: list[str] = []

_index = 0
_module_name = ''
for _index in range(1, 10):
    _module_name = 'chunk%d' % _index
    try:
        _module = __import__('%s.%s' % (__name__, _module_name), fromlist=[_module_name])
    except ImportError:
        _MISSING_CHUNKS.append(_module_name)
        continue
    _chunk = getattr(_module, 'ServiceChunk%d' % _index, None)
    if _chunk is None:
        _MISSING_CHUNKS.append(_module_name)
        continue
    _CHUNK_BASES.append(_chunk)
    _LOADED_CHUNKS.append(_module_name)


class InterludeService(*_CHUNK_BASES):
    """上游 `InterludeService`（`upstream/src/service.ts:624`）。

    MRO 就是 `ServiceChunk0 → ServiceChunk1 → ... → ServiceChunk9 → ServiceBase`，
    因此跨 chunk 调用直接 `self.other_method()` 即可（见分解契约 §4）。

    `_loaded_chunks` / `_missing_chunks` 供适配层在启动时报告分解完整性。
    """

    #: 已混入的 chunk 模块名（按序）。
    _loaded_chunks = tuple(_LOADED_CHUNKS)
    #: 尚未产出（ImportError）的 chunk 模块名，便于启动期一次性告警。
    _missing_chunks = tuple(_MISSING_CHUNKS)


# --------------------------------------------------------------------------- #
# config.py 全量 re-export
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - config.py 由并行任务产出
    from . import config as _config_module
except ImportError:  # pragma: no cover
    _config_module = None

try:  # pragma: no cover - helpers.py 由并行任务产出
    from . import helpers as _helpers_module
except ImportError:  # pragma: no cover
    _helpers_module = None

#: re-export 时跳过的名字：来自 `typing` / `__future__` 等导入，不属于本模块 API。
_REEXPORT_SKIP = frozenset({
    'Any', 'Awaitable', 'Callable', 'Literal', 'Optional', 'Protocol', 'Required',
    'TYPE_CHECKING', 'TypedDict', 'Union', 'annotations', 'cast', 'datetime',
})


def _reexport(module: Any) -> list[str]:
    """把 `module` 的公开符号提升到本包命名空间，返回新加入 `__all__` 的名字。"""
    if module is None:
        return []
    added: list[str] = []
    for name in dir(module):
        if name.startswith('_') or name in _REEXPORT_SKIP:
            continue
        if name in globals():
            continue
        globals()[name] = getattr(module, name)
        added.append(name)
    return added


_REEXPORTED: list[str] = []
_REEXPORTED += _reexport(_config_module)
_REEXPORTED += _reexport(_helpers_module)


# --------------------------------------------------------------------------- #
# desktop_bridge 入口（由 desktop_bridge 移植任务负责）
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - desktop.py 由并行任务产出
    from . import desktop as desktop_bridge
    from .desktop import DesktopBridge, install_desktop_bridge  # noqa: F401
except ImportError:  # pragma: no cover
    desktop_bridge = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
# 明确导出清单（含通配 import 时需要稳定命名的符号）
# --------------------------------------------------------------------------- #

__all__ = [
    'BackgroundDelivery',
    'Config',
    'InterludeContext',
    'InterludeService',
    'NullTransport',
    'SendResult',
    'ServiceBase',
    'ServiceChunk0',
    'SessionView',
    'TimerHandle',
    'Transport',
    'desktop_bridge',
    'is_enabled_account',
    'is_one_bot_platform',
    'legacy_story_id_for',
    'log_fallback',
    'merge_setting',
    'normalize_account_id',
    'normalize_database_row',
    'normalize_group_id',
    'participant_id_for',
    'participant_id_for_story',
    'pick',
    'same_participant_endpoint',
    'story_id_for_character',
]

# config.py / helpers.py 的公开符号也纳入 __all__（并行任务落地后自动生效）。
for _name in _REEXPORTED:
    if _name not in __all__:
        __all__.append(_name)

del _index, _module_name
