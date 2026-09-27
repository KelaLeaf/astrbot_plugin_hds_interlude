"""模型调用治理的测试（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.6）。"""

from __future__ import annotations

import unittest

from plugin.core.llm_governor import (
    GovernorLimits,
    LlmGovernor,
    governor_limits_from_config,
)
from plugin.core.narrator import OpenAICompatibleNarrator, _GovernedHttp


class _Clock:
    """可推进的假时钟：治理器只依赖 `clock()` 与 `sleep()`，测试不用真等。"""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.value += seconds


class GovernorConfigTests(unittest.TestCase):
    def test_governor_is_off_unless_explicitly_enabled(self):
        self.assertIsNone(governor_limits_from_config({}))
        self.assertIsNone(governor_limits_from_config({'governorEnabled': False}))
        self.assertIsNone(governor_limits_from_config(None))
        limits = governor_limits_from_config({'governorEnabled': True})
        self.assertIsNotNone(limits)
        self.assertEqual(limits.max_concurrency, 4)

    def test_limits_are_read_from_both_spellings(self):
        camel = governor_limits_from_config({
            'governorEnabled': True, 'governorMaxConcurrency': 2,
            'governorMaxRequestsPerMinute': 30, 'governorMinCallIntervalMs': 500,
            'governorBreakerFailures': 3, 'governorBreakerCooldownSeconds': 10,
        })
        snake = governor_limits_from_config({
            'governor_enabled': True, 'governor_max_concurrency': 2,
            'governor_max_requests_per_minute': 30, 'governor_min_call_interval_ms': 500,
            'governor_breaker_failures': 3, 'governor_breaker_cooldown_seconds': 10,
        })
        self.assertEqual(camel, snake)
        self.assertEqual(camel.max_requests_per_minute, 30)
        self.assertEqual(camel.min_call_interval_ms, 500)

    def test_nonsense_limits_are_clamped(self):
        limits = GovernorLimits(
            max_concurrency=0, max_requests_per_minute=-5, min_call_interval_ms=-1,
            breaker_failures=0, breaker_cooldown_seconds=0,
        ).normalized()
        self.assertEqual(limits.max_concurrency, 4, '并发 0 会让所有调用都拿不到额度')
        self.assertEqual(limits.max_requests_per_minute, 0)
        self.assertEqual(limits.min_call_interval_ms, 0)
        self.assertEqual(limits.breaker_failures, 5)
        self.assertEqual(limits.breaker_cooldown_seconds, 60.0)

    def test_broken_values_fall_back_instead_of_raising(self):
        limits = governor_limits_from_config({
            'governorEnabled': True, 'governorMaxConcurrency': 'many',
            'governorMaxRequestsPerMinute': None,
        })
        self.assertEqual(limits.max_concurrency, 4)
        self.assertEqual(limits.max_requests_per_minute, 0)


class GovernorAcquireTests(unittest.IsolatedAsyncioTestCase):
    def _governor(self, **overrides) -> tuple[LlmGovernor, _Clock]:
        clock = _Clock()
        limits = GovernorLimits(**overrides).normalized()
        return LlmGovernor(limits, clock=clock.now, sleep=clock.sleep), clock

    async def test_concurrency_cap_blocks_until_a_slot_frees(self):
        governor, _clock = self._governor(max_concurrency=1, interactive_wait_seconds=1.0)
        self.assertIsNone(await governor.acquire('u'))
        blocked = await governor.acquire('u')
        self.assertIn('并发已满', blocked)
        governor.release('u')
        self.assertIsNone(await governor.acquire('u'))

    async def test_lanes_do_not_share_slots(self):
        governor, _clock = self._governor(max_concurrency=1)
        self.assertIsNone(await governor.acquire('a'))
        self.assertIsNone(await governor.acquire('b'), '不同连接各有一条车道')

    async def test_min_interval_waits_between_calls(self):
        governor, clock = self._governor(max_concurrency=4, min_call_interval_ms=500)
        self.assertIsNone(await governor.acquire('u'))
        governor.release('u')
        self.assertIsNone(await governor.acquire('u'))
        self.assertEqual(clock.slept, [0.5], '第二次调用要先等满最小间隔')

    async def test_per_minute_window_waits_then_recovers(self):
        governor, clock = self._governor(max_concurrency=4, max_requests_per_minute=2)
        for _ in range(2):
            self.assertIsNone(await governor.acquire('u'))
            governor.release('u')
        blocked = await governor.acquire('u')
        self.assertIn('每分钟上限', blocked)
        clock.value += 61
        self.assertIsNone(await governor.acquire('u'))

    async def test_background_call_yields_to_an_interactive_waiter(self):
        governor, _clock = self._governor(max_concurrency=1, background_wait_seconds=0.0)
        self.assertIsNone(await governor.acquire('u'), '实时回合先占住唯一额度')
        governor.lane('u').interactive_waiters = 1
        blocked = await governor.acquire('u', background=True)
        self.assertIn('让位', blocked)
        governor.lane('u').interactive_waiters = 0
        governor.release('u')
        self.assertIsNone(await governor.acquire('u', background=True))

    async def test_interactive_call_fails_fast_when_it_cannot_wait(self):
        governor, _clock = self._governor(
            max_concurrency=1, interactive_wait_seconds=0.0, background_wait_seconds=0.0,
        )
        self.assertIsNone(await governor.acquire('u'))
        self.assertIn('并发已满', await governor.acquire('u'))

    async def test_the_waiter_counter_is_always_released(self):
        governor, _clock = self._governor(max_concurrency=1)
        self.assertIsNone(await governor.acquire('u'))
        await governor.acquire('u')
        self.assertEqual(governor.lane('u').interactive_waiters, 0)


class GovernorBreakerTests(unittest.IsolatedAsyncioTestCase):
    def _governor(self, **overrides) -> tuple[LlmGovernor, _Clock]:
        clock = _Clock()
        limits = GovernorLimits(**overrides).normalized()
        return LlmGovernor(limits, clock=clock.now, sleep=clock.sleep), clock

    async def test_consecutive_failures_open_the_breaker(self):
        governor, clock = self._governor(breaker_failures=2, breaker_cooldown_seconds=30)
        for _ in range(2):
            governor.report_failure('u')
        self.assertTrue(governor.breaker_open('u'))
        blocked = await governor.acquire('u')
        self.assertIn('熔断中', blocked)
        clock.value += 31
        self.assertFalse(governor.breaker_open('u'), '冷却结束放探针')
        self.assertIsNone(await governor.acquire('u'))

    async def test_a_success_resets_the_failure_count(self):
        governor, _clock = self._governor(breaker_failures=2)
        governor.report_failure('u')
        governor.report_success('u')
        governor.report_failure('u')
        self.assertFalse(governor.breaker_open('u'))

    async def test_breakers_are_per_lane(self):
        governor, _clock = self._governor(breaker_failures=1)
        governor.report_failure('a')
        self.assertIn('熔断中', await governor.acquire('a'))
        self.assertIsNone(await governor.acquire('b'))


class _RecordingHttp:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.error = error

    async def post_json(self, url, headers, body, timeout=None, task=None):
        self.calls.append((url, task))
        if self.error is not None:
            raise self.error
        return {'ok': True}

    async def iterate_sse(self, *args, **kwargs):
        """`resolve_http` 的鸭子类型要求这个成员存在。"""
        if False:  # pragma: no cover - 只是异步生成器
            yield None

    def other_method(self) -> str:
        return 'passthrough'


class GovernedHttpProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_proxy_passes_through_other_members(self):
        proxy = _GovernedHttp(_RecordingHttp(), LlmGovernor(GovernorLimits()))
        self.assertEqual(proxy.other_method(), 'passthrough')

    async def test_calls_pass_through_and_release_the_slot(self):
        inner = _RecordingHttp()
        governor = LlmGovernor(GovernorLimits(max_concurrency=1))
        proxy = _GovernedHttp(inner, governor)
        self.assertEqual(await proxy.post_json('u', {}, {}, task='main'), {'ok': True})
        self.assertEqual(governor.lane('u').running, 0, '成功也要归还额度')
        self.assertEqual(inner.calls, [('u', 'main')])

    async def test_a_failure_is_reported_and_still_releases(self):
        inner = _RecordingHttp(RuntimeError('boom'))
        governor = LlmGovernor(GovernorLimits(max_concurrency=1, breaker_failures=1))
        proxy = _GovernedHttp(inner, governor)
        with self.assertRaises(RuntimeError):
            await proxy.post_json('u', {}, {})
        self.assertEqual(governor.lane('u').running, 0)
        self.assertEqual(governor.lane('u').failures, 1)

    async def test_a_blocked_call_raises_before_touching_the_provider(self):
        inner = _RecordingHttp()
        # 这里用真实 `asyncio.sleep`，所以把等待上限设成 0，别让测试真的等 20 秒。
        governor = LlmGovernor(GovernorLimits(max_concurrency=1, interactive_wait_seconds=0.0))
        await governor.acquire('u')  # 占住唯一额度且不释放
        proxy = _GovernedHttp(inner, governor)
        with self.assertRaises(RuntimeError) as caught:
            await proxy.post_json('u', {}, {})
        self.assertIn('治理器拦下', str(caught.exception))
        self.assertEqual(inner.calls, [])

    async def test_the_narrator_only_wraps_when_the_governor_is_on(self):
        off = OpenAICompatibleNarrator(_RecordingHttp(), {}, silent_logs=True)
        self.assertIsNone(off.governor)
        self.assertNotIsInstance(off.http, _GovernedHttp)
        # 叙事器拿到的是**model 段本身**（内部已归一成 snake_case），治理键就在这一层。
        on = OpenAICompatibleNarrator(
            _RecordingHttp(), {'governor_enabled': True, 'governor_max_concurrency': 2},
            silent_logs=True,
        )
        self.assertIsNotNone(on.governor)
        self.assertIsInstance(on.http, _GovernedHttp)


if __name__ == '__main__':
    unittest.main(verbosity=2)
