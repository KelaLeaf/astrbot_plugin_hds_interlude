"""上游 `src/model-routing.ts` 的 Python 对应物：模型任务路由表。

移植自 Koishi / TypeScript 上游快照 `upstream/src/model-routing.ts`
（上游版本 `1.0.1-beta6-rebuild`，213 行）。本模块把「连接（provider）」与
「任务（main / compaction / timeline / alter / embedding / stickers / vision）」
的匹配规则集中在一处，产出静态候选序列表；运行期的冷却重排、round-robin
轮换由调用方（`core/narrator.py`）在候选列表之上进行——本文件只负责
**静态、按配置顺序** 的挑选，与上游完全一致。

语言映射约定（详见移植约定）：
- 函数 `camelCase` → `snake_case`；配置/领域对象的字段名同样 snake_case
  （与 `core/types.py` 的 `AlterSystemConfig` 等保持一致：`model_id`、
  `provider_id`、`max_tokens`、`response_format`、`use_for_main` …）。
- 上游每个 `{ ... }` 字面量 → Python `dict`；`ResolvedModelTarget` /
  `ResolvedModelRoute` / `ModelRoutingTable` 用 `TypedDict` 标注（运行期零开销）。
- 上游 `ProviderConfig` / `ModelConfig` / `ModelProfile` / `ProviderMode` /
  `ProviderResponseFormat` 定义在 `src/narrator.ts`。本模块只在类型检查期
  （`TYPE_CHECKING`）import 它们，运行期不依赖 `core/narrator.py`，
  这样两个模块可以并行落地、也不会产生 import 环。
- 上游 `provider?.label?.trim() || x` → `_trim()` + `_or()`：
  显式复刻 JS 的「可选链 + 真值或」语义（空串 / None 都算假值）。
- 上游 `?? `（空值合并）→ `_coalesce()`：**只有 None**（= JS undefined/null）
  才回落到默认值，`0` / `''` 一律保留。
- 上游 `x === true` / `x === false` → `is True` / `is False`：严格相等，
  与 JS 一致（`1 == true` 在 JS 里是 false，这里也不接受 1）。
- `isAssignedTo(provider, task)` 的类型签名把 `'timeline'` 排除在外
  （timeline 复用 compaction 的候选，没有自己的开关）；运行期若真传入
  `'timeline'`，上游的三元链会一路落到最后一项，即 `useForVision === true`，
  这里保持同样的运行期行为。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    # 定义在 `src/narrator.ts`（本仓库 `core/narrator.py`，并行移植中）。
    # 仅类型检查期可见：本模块的公开函数运行期不需要这些名字。
    from .narrator import (
        ModelConfig,
        ModelProfile,
        ProviderConfig,
        ProviderMode,
        ProviderResponseFormat,
    )
    from .types import AlterSystemConfig

__all__ = [
    'ModelTask',
    'ResolvedModelTarget',
    'ResolvedModelRoute',
    'ModelRoutingTable',
    'ZHIPU_OFFICIAL_CHAT_ENDPOINT',
    'resolve_model_routing',
    'resolve_model_target',
    'effective_main_model_id',
    'configured_providers',
    'uses_remote_providers',
    'provider_key',
    'provider_reachable',
    'is_assigned_to',
    'format_model_routing',
]

ModelTask = Literal['main', 'compaction', 'timeline', 'alter', 'embedding', 'stickers', 'vision']


class ResolvedModelTarget(TypedDict, total=False):
    """一次任务请求最终指向的连接与模型（上游 `ResolvedModelTarget`）。"""

    provider_id: str
    model: str
    max_tokens: int
    timeout: int
    response_format: ProviderResponseFormat


class ResolvedModelRoute(TypedDict, total=False):
    """某个任务的解析结果（上游 `ResolvedModelRoute`）。"""

    task: ModelTask
    target: ResolvedModelTarget
    # 有序静态候选。运行期冷却可能临时重排它们。
    providers: list[ProviderConfig]
    assigned: bool
    available: bool
    reason: Literal[
        'assigned-provider', 'model-profile', 'task-config', 'legacy-fallback', 'disabled', 'unavailable',
    ]


class ModelRoutingTable(TypedDict, total=False):
    """七个任务的完整路由表（上游 `ModelRoutingTable`）。"""

    providers: list[ProviderConfig]
    main: ResolvedModelRoute
    compaction: ResolvedModelRoute
    timeline: ResolvedModelRoute
    alter: ResolvedModelRoute
    embedding: ResolvedModelRoute
    stickers: ResolvedModelRoute
    vision: ResolvedModelRoute


ZHIPU_OFFICIAL_CHAT_ENDPOINT = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'


def resolve_model_routing(config: ModelConfig, alter_config: AlterSystemConfig | None = None) -> ModelRoutingTable:
    """上游 `resolveModelRouting()`：产出整张路由表。

    `alter_config` 为 None 时（上游 `alterConfig?` 未传），alter 任务回落到
    主模型；与 `usesRemoteProviders()` 的内部调用一致。
    """
    providers = configured_providers(config)
    main_target = resolve_model_target(config, effective_main_model_id(config), '', '')
    compact = _as_mapping(config.get('compaction'))
    compaction_target = resolve_model_target(
        config,
        _or(_get(compact, 'model_id'), effective_main_model_id(config)),
        _get(compact, 'provider_id'),
        _get(compact, 'model'),
    )
    alter_target = resolve_model_target(
        config,
        _or(_get(alter_config, 'model_id'), effective_main_model_id(config)),
        _get(alter_config, 'provider_id'),
        _get(alter_config, 'model'),
    )
    embedding = _as_mapping(config.get('embedding'))
    embedding_target = resolve_model_target(
        config, _get(embedding, 'model_id'), _get(embedding, 'provider_id'), _get(embedding, 'model'),
    )

    main = resolve_route('main', providers, main_target, True)
    compaction = disabled_route('compaction', compaction_target) if _is_false(_get(compact, 'enabled')) \
        else resolve_route('compaction', providers, compaction_target, True)
    return {
        'providers': providers,
        'main': main,
        'compaction': compaction,
        # timeline 复用 compaction 的候选，只换任务名（上游 `{ ...compaction, task: 'timeline' }`）。
        'timeline': {**compaction, 'task': 'timeline'},
        'alter': disabled_route('alter', alter_target) if _is_false(_get(alter_config, 'enabled'))
        else resolve_route('alter', providers, alter_target, True),
        'embedding': resolve_route('embedding', providers, embedding_target, False)
        if _truthy(_get(embedding, 'enabled'))
        else disabled_route('embedding', embedding_target),
        'stickers': resolve_assigned_only_route('stickers', providers),
        'vision': resolve_assigned_only_route('vision', providers),
    }


def resolve_model_target(
    config: ModelConfig,
    model_id: str | None,
    provider_id: str | None,
    model: str | None,
) -> ResolvedModelTarget:
    """上游 `resolveModelTarget()`：把一个 model profile id 解析成连接 + 模型。

    `max_tokens` / `timeout` / `response_format` 在 JS 里恒存在（可能是
    undefined），这里同样恒带键（值为 None），保持 `'max_tokens' in target`
    这类判断的语义与上游一致。
    """
    selected: ModelProfile | None = None
    trimmed_id = _trim(model_id)
    if trimmed_id:
        for entry in _as_list(config.get('models')):
            if _is_false(_get(entry, 'enabled')):
                continue
            if _get(entry, 'id') == trimmed_id:
                selected = entry
                break
    return {
        'provider_id': _or(_trim(_get(selected, 'provider_id')), _trim(provider_id)),
        'model': _or(_trim(_get(selected, 'model')), _trim(model)),
        'max_tokens': _get(selected, 'max_tokens'),
        'timeout': _get(selected, 'timeout'),
        'response_format': _get(selected, 'response_format'),
    }


def effective_main_model_id(config: ModelConfig) -> str:
    """上游 `effectiveMainModelId()`：显式 mainModelId 优先，否则唯一的可用 profile。"""
    explicit = _trim(config.get('main_model_id'))
    if explicit:
        return explicit
    available = enabled_model_profiles(config)
    return available[0]['id'] if len(available) == 1 else ''


def configured_providers(config: ModelConfig) -> list[ProviderConfig]:
    """上游 `configuredProviders()`：把配置里的连接逐条归一化。"""
    return [normalize_provider(provider) for provider in _as_list(config.get('providers'))]


def uses_remote_providers(config: ModelConfig) -> bool:
    """上游 `usesRemoteProviders()`：只要有任一任务可用远端连接就算远端模式。"""
    routing = resolve_model_routing(config)
    return bool(
        _truthy(_get(routing['main'], 'available'))
        or _truthy(_get(routing['compaction'], 'available'))
        or _truthy(_get(routing['embedding'], 'available'))
        or _truthy(_get(routing['stickers'], 'available'))
        or _truthy(_get(routing['vision'], 'available'))
    )


def provider_key(provider: ProviderConfig) -> str:
    """上游 `providerKey()`：连接的稳定去重键。

    显式 id 优先；否则用 `label:model:endpoint` 三元组——这三项恰好是
    「同一个连接」的判定依据，因此没有 id 的历史配置也能正确去重。
    """
    return _or(_trim(provider.get('id')), '{}:{}:{}'.format(
        _trim(provider.get('label')), _trim(provider.get('model')), _trim(provider.get('endpoint')),
    ))


def provider_reachable(provider: ProviderConfig) -> bool:
    """这条连接行当前有没有可用的请求目标。

    上游只看 http(s) `endpoint`。本移植版多认一条：连接行可以声明
    `transport_target`——它的目标由**传输层**解析（例如"这个任务用宿主自己管理的
    某个模型"），此时插件侧本来就不需要、也不该填地址。core 只判断真值，
    不关心那个字符串是什么；把它塞进去的是适配层。

    没有这条的话，"不填连接、只用宿主的模型"会在 core 就被判成 unavailable
    （`No enabled OpenAI-compatible provider is available.`），传输层的回退路径
    永远走不到。
    """
    return bool(_truthy(provider.get('endpoint')) or _truthy(provider.get('transport_target')))


def is_assigned_to(provider: ProviderConfig, task: str) -> bool:
    """上游 `isAssignedTo()`：这条连接是否被显式指派给该任务。

    上游签名把 `'timeline'` 排除在合法任务之外（timeline 没有独立开关，
    它跟随 compaction）；运行期若真传入 `'timeline'`，上游会落到三元链
    最后一项 `useForVision === true`，这里保持同样的行为。
    """
    if task == 'main':
        return provider.get('use_for_main') is True
    if task == 'compaction':
        return provider.get('use_for_compaction') is True
    if task == 'alter':
        return provider.get('use_for_alter') is True
    if task == 'embedding':
        return provider.get('use_for_embedding') is True
    if task == 'stickers':
        return provider.get('use_for_stickers') is True
    return provider.get('use_for_vision') is True


def format_model_routing(table: ModelRoutingTable) -> str:
    """上游 `formatModelRouting()`：一行人类可读的路由摘要（启动日志用）。"""
    tasks: list[ModelTask] = ['main', 'compaction', 'timeline', 'alter', 'embedding', 'stickers', 'vision']
    parts: list[str] = []
    for task in tasks:
        route = table[task]
        providers = _as_list(_get(route, 'providers'))
        provider = providers[0] if providers else None
        if _truthy(_get(route, 'assigned')):
            model = _get(provider, 'model')
        else:
            model = _or(_get(_get(route, 'target'), 'model'), _get(provider, 'model'))
        if _truthy(_get(route, 'available')):
            detail = '{}/{}'.format(
                _or(_get(provider, 'label'), _get(provider, 'id')),
                _or(model, '未指定'),
            )
        else:
            detail = '未配置'
        parts.append('{}={}[{}]'.format(task, detail, _get(route, 'reason')))
    return ' '.join(parts)


# ========== 内部辅助（上游未 export 的部分） ==========


def resolve_route(
    task: str,
    providers: list[ProviderConfig],
    target: ResolvedModelTarget,
    require_chat_model: bool,
) -> ResolvedModelRoute:
    """上游 `resolveRoute()`：显式指派 → 指定连接/模型 → 历史兜底。"""
    assigned = [
        provider for provider in providers
        if _truthy(provider.get('enabled')) and provider_reachable(provider) and _truthy(provider.get('model'))
        and is_assigned_to(provider, task)
    ]
    if assigned:
        return {
            'task': task, 'target': target, 'providers': assigned,
            'assigned': True, 'available': True, 'reason': 'assigned-provider',
        }

    targeted = [
        provider for provider in providers
        if _truthy(provider.get('enabled')) and provider_reachable(provider)
        and (provider.get('id') == target.get('provider_id') or provider_key(provider) == target.get('provider_id'))
    ] if _truthy(target.get('provider_id')) else []
    targeted_usable = [
        provider for provider in targeted
        if _truthy(_or(target.get('model'), provider.get('model')))
    ]
    if targeted_usable:
        profile_selected = _truthy(target.get('provider_id')) and _truthy(target.get('model'))
        return {
            'task': task, 'target': target, 'providers': targeted_usable,
            'assigned': False, 'available': True,
            'reason': 'model-profile' if profile_selected else 'task-config',
        }

    # 让历史安装继续可用，但绝不把「只做 embedding 的连接」当成聊天兜底。
    # 显式的任务指派仍然是首选。
    fallback = [
        provider for provider in providers
        if _truthy(provider.get('enabled')) and provider_reachable(provider)
        and (not require_chat_model or _truthy(provider.get('model')))
        and not is_exclusively_non_chat(provider)
    ]
    if fallback:
        return {
            'task': task, 'target': target, 'providers': fallback,
            'assigned': False, 'available': True, 'reason': 'legacy-fallback',
        }
    return {
        'task': task, 'target': target, 'providers': [],
        'assigned': False, 'available': False, 'reason': 'unavailable',
    }


def resolve_assigned_only_route(task: str, providers: list[ProviderConfig]) -> ResolvedModelRoute:
    """上游 `resolveAssignedOnlyRoute()`：stickers / vision 只认显式指派。

    它们没有兜底路径——把主模型拿去描述表情包或图片是错误行为。
    """
    assigned = [
        provider for provider in providers
        if _truthy(provider.get('enabled')) and provider_reachable(provider) and _truthy(provider.get('model'))
        and is_assigned_to(provider, task)
    ]
    return {
        'task': task,
        'target': {
            'provider_id': _coalesce(_get(assigned[0], 'id') if assigned else None, ''),
            'model': _coalesce(_get(assigned[0], 'model') if assigned else None, ''),
        },
        'providers': assigned,
        'assigned': len(assigned) > 0,
        'available': len(assigned) > 0,
        'reason': 'assigned-provider' if assigned else 'unavailable',
    }


def disabled_route(task: str, target: ResolvedModelTarget) -> ResolvedModelRoute:
    """上游 `disabledRoute()`：功能被显式关闭。"""
    return {'task': task, 'target': target, 'providers': [], 'assigned': False, 'available': False, 'reason': 'disabled'}


def is_exclusively_non_chat(provider: ProviderConfig) -> bool:
    """上游 `isExclusivelyNonChat()`：只被指派给旁路任务（embedding/表情/视觉）。"""
    chat = _truthy(provider.get('use_for_main')) or _truthy(provider.get('use_for_compaction')) \
        or _truthy(provider.get('use_for_alter'))
    sidecar = _truthy(provider.get('use_for_embedding')) or _truthy(provider.get('use_for_stickers')) \
        or _truthy(provider.get('use_for_vision'))
    return bool(sidecar and not chat)


def enabled_model_profiles(config: ModelConfig) -> list[ModelProfile]:
    """上游 `enabledModelProfiles()`：可用的中央模型目录条目（id/连接/模型齐全）。"""
    return [
        entry for entry in _as_list(config.get('models'))
        if not _is_false(_get(entry, 'enabled'))
        and _trim(_get(entry, 'id')) and _trim(_get(entry, 'provider_id')) and _trim(_get(entry, 'model'))
    ]


def normalize_provider(provider: ProviderConfig) -> ProviderConfig:
    """上游 `normalizeProvider()`：补齐缺省值、推断官方 endpoint、归一化连接身份。

    刻意保留上游未声明但可能存在的额外字段（计费价目 `price_input` 等），
    对应上游的 `{ ...provider, ... }` 展开。
    """
    zhipu_official = provider.get('mode') == 'zhipu-official'
    deepseek_official = provider.get('mode') == 'deepseek-official'
    official_endpoint = preset_endpoint(provider.get('mode'), provider.get('dashscope_region'))
    normalized: ProviderConfig = dict(provider)  # type: ignore[assignment]
    normalized.update({
        'id': _or(_trim(provider.get('id')), '{}:{}'.format(
            _or(_trim(provider.get('label')), 'provider'), _trim(provider.get('model')),
        )),
        'label': _or(_trim(provider.get('label')), (
            'Zhipu Official' if zhipu_official else 'DeepSeek Official' if deepseek_official else 'Model connection'
        )),
        'endpoint': _or(official_endpoint, provider.get('endpoint')),
        'api_key': _coalesce(provider.get('api_key'), ''),
        'model': _coalesce(provider.get('model'), ''),
        'temperature': _coalesce(provider.get('temperature'), 1 if zhipu_official else 0.8),
        'top_p': _coalesce(provider.get('top_p'), 0.95 if zhipu_official else 1),
        'max_tokens': _coalesce(provider.get('max_tokens'), 4096),
        'timeout': _coalesce(provider.get('timeout'), 45_000 if zhipu_official else 60_000),
        'response_format': _coalesce(provider.get('response_format'), 'json-object'),
        'extra_headers': _coalesce(provider.get('extra_headers'), ''),
        'extra_body': _coalesce(provider.get('extra_body'), ''),
        # 由适配层塞入的不透明句柄：非空表示"目标由传输层解析"，见 provider_reachable()
        'transport_target': _coalesce(provider.get('transport_target'), ''),
        'zhipu_official': zhipu_official,
        'reasoning_effort': _coalesce(provider.get('reasoning_effort'), 'high'),
        'deepseek_official': deepseek_official,
        'deepseek_thinking': 'enabled' if provider.get('deepseek_thinking') == 'enabled' else 'disabled',
        'deepseek_reasoning_effort': _coalesce(provider.get('deepseek_reasoning_effort'), 'low'),
        'use_for_main': provider.get('use_for_main') is True,
        'use_for_compaction': provider.get('use_for_compaction') is True,
        'use_for_alter': provider.get('use_for_alter') is True,
        'use_for_embedding': provider.get('use_for_embedding') is True,
        'use_for_stickers': provider.get('use_for_stickers') is True,
        'use_for_vision': provider.get('use_for_vision') is True,
    })
    return normalized


def preset_endpoint(mode: ProviderMode | None, dashscope_region: str | None = None) -> str:
    """上游 `presetEndpoint()`：官方/托管模式的固定 endpoint。

    未识别模式返回空串，调用方据此保留用户自填的 `endpoint`。
    """
    if mode == 'zhipu-official':
        return ZHIPU_OFFICIAL_CHAT_ENDPOINT
    if mode == 'openai-official':
        return 'https://api.openai.com/v1/chat/completions'
    if mode == 'deepseek-official':
        return 'https://api.deepseek.com/v1/chat/completions'
    if mode == 'moonshot-official':
        return 'https://api.moonshot.cn/v1/chat/completions'
    if mode == 'siliconflow-official':
        return 'https://api.siliconflow.cn/v1/chat/completions'
    if mode == 'openrouter':
        return 'https://openrouter.ai/api/v1/chat/completions'
    if mode == 'gemini-openai':
        return 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    if mode == 'dashscope-official':
        if dashscope_region == 'singapore':
            return 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions'
        if dashscope_region == 'us':
            return 'https://dashscope-us.aliyuncs.com/compatible-mode/v1/chat/completions'
        return 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
    return ''


def _get(obj: Any, key: str) -> Any:
    """JS 的属性读取：对象缺失时返回 undefined（这里用 None 表示）。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return None


def _as_list(value: Any) -> list[Any]:
    """JS 里 `config.providers` / `config.models` 缺失即为 undefined。"""
    return value if isinstance(value, list) else []


def _as_mapping(value: Any) -> dict[str, Any] | None:
    """`compaction?` / `embedding?` / `alterConfig?` 这类可选子对象。"""
    return value if isinstance(value, dict) else None


def _trim(value: Any) -> str:
    """JS `value?.trim()`：非字符串（含 undefined）一律得到空串。"""
    return value.strip() if isinstance(value, str) else ''


def _truthy(value: Any) -> bool:
    """JS 真值语义：None/false/0/''/NaN 为假，**空数组与空对象为真**。"""
    if value is None or value is False:
        return False
    if isinstance(value, (list, tuple, dict, set)):
        return True
    return bool(value)


def _or(left: Any, right: Any) -> Any:
    """JS `||`：左侧为假值（含空串）时取右侧。"""
    return left if _truthy(left) else right


def _coalesce(value: Any, default: Any) -> Any:
    """JS `??`：只有 None（undefined/null）才回落，`0` / `''` 保留。"""
    return default if value is None else value


def _is_false(value: Any) -> bool:
    """JS `x === false`：严格相等，`0` / `''` 不算 false。"""
    return value is False
