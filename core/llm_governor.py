"""模型调用的治理器：并发、频率、最小间隔、后台让位、熔断（v1.4.0）。

为什么需要它：主叙事、压缩、记忆维护、场景压缩、Alter、向量化、识图**共用同一批连接**。
没有治理时三件事会同时发生：

1. 后台任务（压缩 / 维护 / 时间导演）和用户的实时回合撞在一起排队，用户明显感到变慢；
2. 便宜的小模型连接被 RPM 打满以后，主叙事跟着一起 429；
3. 某个连接挂了之后，每一轮都要先白等一次超时才 failover。

参考实现（Iris Memory 的 LLM governor）有全局与 Provider 两级并发、RPM、最小间隔、
优先级 aging、有界队列与熔断。本移植版按我们的形状取其中的四件：
**并发上限、每分钟上限、调用最小间隔、连续失败熔断**，外加"后台让位给实时回合"。

设计约束（与仓库其它部分一致）：

- **核心不 import astrbot**，也不依赖 asyncio 之外的东西；时钟与 sleep 可注入，测试不用真等；
- 默认**关闭**（`governor_enabled`）：这是会改变既有节奏的行为变更，不该在升级时静默生效；
- 拿不到额度时**不阻塞到天荒地老**：等不到就返回 `None`，调用方按"这一轮先跳过"处理。
  后台任务本来就可以等下一轮；实时回合宁可失败一次也不要卡住用户。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = ['GovernorLimits', 'LlmGovernor', 'governor_limits_from_config']

#: 默认单连接并发上限。四路足够让"实时回合 + 一路后台"并行，又不会把免费额度打爆。
DEFAULT_MAX_CONCURRENCY = 4

#: 熔断：连续失败到这个次数就打开，冷却结束后放一个探针请求过去。
DEFAULT_BREAKER_FAILURES = 5
DEFAULT_BREAKER_COOLDOWN_SECONDS = 60.0

#: 实时回合最多等这么久（秒）；后台任务等这么久还拿不到就让位。
DEFAULT_INTERACTIVE_WAIT_SECONDS = 20.0
DEFAULT_BACKGROUND_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class GovernorLimits:
    """一处治理参数（从 `model_center` 的配置读出来）。"""

    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    max_requests_per_minute: int = 0
    min_call_interval_ms: int = 0
    breaker_failures: int = DEFAULT_BREAKER_FAILURES
    breaker_cooldown_seconds: float = DEFAULT_BREAKER_COOLDOWN_SECONDS
    interactive_wait_seconds: float = DEFAULT_INTERACTIVE_WAIT_SECONDS
    background_wait_seconds: float = DEFAULT_BACKGROUND_WAIT_SECONDS

    def normalized(self) -> 'GovernorLimits':
        """把不可能的取值夹回可用范围（配置是用户填的，什么都可能）。"""
        return GovernorLimits(
            max_concurrency=max(1, int(self.max_concurrency or DEFAULT_MAX_CONCURRENCY)),
            max_requests_per_minute=max(0, int(self.max_requests_per_minute or 0)),
            min_call_interval_ms=max(0, int(self.min_call_interval_ms or 0)),
            breaker_failures=max(1, int(self.breaker_failures or DEFAULT_BREAKER_FAILURES)),
            breaker_cooldown_seconds=max(1.0, float(self.breaker_cooldown_seconds or DEFAULT_BREAKER_COOLDOWN_SECONDS)),
            interactive_wait_seconds=max(0.0, float(self.interactive_wait_seconds)),
            background_wait_seconds=max(0.0, float(self.background_wait_seconds)),
        )


def governor_limits_from_config(config: Any) -> Optional[GovernorLimits]:
    """从 `model_center` 段读出治理参数；没开就返回 `None`（调用方不做任何包装）。"""
    if not isinstance(config, dict):
        return None
    enabled = config.get('governorEnabled', config.get('governor_enabled'))
    if enabled is not True:
        return None
    limits = GovernorLimits(
        max_concurrency=_int(config, 'governorMaxConcurrency', 'governor_max_concurrency', DEFAULT_MAX_CONCURRENCY),
        max_requests_per_minute=_int(config, 'governorMaxRequestsPerMinute', 'governor_max_requests_per_minute', 0),
        min_call_interval_ms=_int(config, 'governorMinCallIntervalMs', 'governor_min_call_interval_ms', 0),
        breaker_failures=_int(config, 'governorBreakerFailures', 'governor_breaker_failures', DEFAULT_BREAKER_FAILURES),
        breaker_cooldown_seconds=_int(
            config, 'governorBreakerCooldownSeconds', 'governor_breaker_cooldown_seconds',
            int(DEFAULT_BREAKER_COOLDOWN_SECONDS),
        ),
    )
    return limits.normalized()


def _int(config: dict, camel: str, snake: str, fallback: int) -> int:
    value = config.get(camel, config.get(snake))
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


@dataclass
class _Lane:
    """单个连接（按 URL 分）的运行状态。"""

    running: int = 0
    stamps: deque = field(default_factory=deque)
    last_started: Optional[float] = None
    failures: int = 0
    opened_until: Optional[float] = None
    interactive_waiters: int = 0


class LlmGovernor:
    """按连接（URL）分组的限流与熔断。

    用法：

        lease = await governor.acquire(url, background=task != 'main')
        if lease is None:   # 没拿到额度 / 被熔断 → 这一轮跳过
            ...
        try:
            response = await http.post_json(...)
        except Exception:
            governor.report_failure(url)
            raise
        else:
            governor.report_success(url)
    """

    def __init__(
        self, limits: Optional[GovernorLimits] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self.limits = (limits or GovernorLimits()).normalized()
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._lanes: dict[str, _Lane] = {}

    # ---- 状态查询（测试与日志用） ----

    def lane(self, key: str) -> _Lane:
        return self._lanes.setdefault(str(key or ''), _Lane())

    def breaker_open(self, key: str) -> bool:
        lane = self.lane(key)
        if lane.opened_until is None:
            return False
        if self._clock() >= lane.opened_until:
            # 冷却结束：放一个探针过去（半开），它失败就再次打开。
            lane.opened_until = None
            lane.failures = 0
            return False
        return True

    # ---- 额度 ----

    async def acquire(self, key: str, background: bool = False) -> Optional[str]:
        """尝试拿到一次调用额度。

        返回 `None` 表示拿到了；返回字符串是**没拿到**的原因（写进日志/诊断用），
        调用方据此决定跳过还是 failover。
        """
        normalized = str(key or '')
        lane = self.lane(normalized)
        deadline = self._clock() + (
            self.limits.background_wait_seconds if background else self.limits.interactive_wait_seconds
        )
        if not background:
            lane.interactive_waiters += 1
        try:
            while True:
                if self.breaker_open(normalized):
                    return '熔断中（连续失败 %d 次）' % lane.failures
                # 后台任务让位：只要有实时回合在等，后台就别再占额度。这一步必须在
                # 并发检查**之前**——否则并发满了以后后台只会看到"并发已满"，
                # 让位就成了永远走不到的分支（测试先抓到的就是这个）。
                if background and lane.interactive_waiters > 0:
                    if not await self._wait(deadline):
                        return '让位给实时回合'
                    continue
                if lane.running >= self.limits.max_concurrency:
                    if not await self._wait(deadline):
                        return '并发已满（%d）' % self.limits.max_concurrency
                    continue
                wait = self._interval_wait(lane)
                if wait > 0:
                    if self._clock() + wait > deadline:
                        return '调用间隔未到（还需 %.0fms）' % (wait * 1000)
                    await self._sleep(wait)
                    continue
                window_wait = self._window_wait(lane)
                if window_wait > 0:
                    if self._clock() + window_wait > deadline:
                        return '每分钟上限已满（%d）' % self.limits.max_requests_per_minute
                    await self._sleep(window_wait)
                    continue
                lane.running += 1
                lane.last_started = self._clock()
                lane.stamps.append(self._clock())
                return None
        finally:
            if not background:
                lane.interactive_waiters = max(0, lane.interactive_waiters - 1)

    def release(self, key: str) -> None:
        """归还一次调用额度（无论成功失败都要还）。"""
        lane = self.lane(str(key or ''))
        lane.running = max(0, lane.running - 1)

    # ---- 结果回报 ----

    def report_success(self, key: str) -> None:
        lane = self.lane(str(key or ''))
        lane.failures = 0
        lane.opened_until = None

    def report_failure(self, key: str) -> None:
        lane = self.lane(str(key or ''))
        lane.failures += 1
        if lane.failures >= self.limits.breaker_failures:
            lane.opened_until = self._clock() + self.limits.breaker_cooldown_seconds

    # ---- 内部计算 ----

    def _interval_wait(self, lane: _Lane) -> float:
        interval = self.limits.min_call_interval_ms / 1000.0
        if interval <= 0 or lane.last_started is None:
            return 0.0
        return max(0.0, interval - (self._clock() - lane.last_started))

    def _window_wait(self, lane: _Lane) -> float:
        limit = self.limits.max_requests_per_minute
        if limit <= 0:
            return 0.0
        now = self._clock()
        while lane.stamps and now - lane.stamps[0] >= 60.0:
            lane.stamps.popleft()
        if len(lane.stamps) < limit:
            return 0.0
        return max(0.0, 60.0 - (now - lane.stamps[0]))

    async def _wait(self, deadline: float) -> bool:
        """等一小会儿；超过截止时间返回 False（调用方据此放弃本轮）。"""
        if self._clock() >= deadline:
            return False
        await self._sleep(min(0.05, max(0.0, deadline - self._clock())))
        return self._clock() < deadline
