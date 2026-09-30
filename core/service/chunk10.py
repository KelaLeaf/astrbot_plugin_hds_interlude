"""Chunk10 mixin：上游 1.0.1-rc23 起的**服务层新增段落**（原 `src/service.ts` 7084 行之后）。

上游在这条线上把 service.ts 从 7084 行涨到了约 9500 行（世界播种器、端点注册表与
通道标注的 service 侧）。移植版把 chunk0..9 按上游**旧行段**切分，于是 rc28 新增的
服务层收在这一片里——约定与其它 chunk 相同：跨 mixin 调用一律 `self.其他方法()`。

| 上游 | 本文件 |
| --- | --- |
| `worldSeederRuntime`（service.ts:780-790） | `world_seeder_runtime` / `world_seeder_available` |
| `worldSeederSweep()`（:7458） | `world_seeder_sweep` |
| `drainDueSeededEvents()`（:7556） | `drain_due_seeded_events` |
| `buildWorldSeederPayload()`（world-seeder.ts） | `_build_world_seeder_payload` |

纯函数层在 `core/world_seeder.py`（切面轮换 / Jaccard 去重 / 六道校验闸 / 系统提示词）；
本文件只负责门序、频控、数据库与剧本条目注入。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any, Optional

from ..time import dt_ms, iso, parse_dt
from ..world_seeder import (
    STATUS_EXPIRED,
    STATUS_INJECTED,
    STATUS_INJECTING,
    STATUS_SCHEDULED,
    parse_world_seed_events,
    resolve_world_seeder_runtime,
    season_for_month,
    seed_domain_for_run,
    validate_seed_event,
    world_seeder_system_prompt,
)
from .base import ServiceBase, pick

__all__ = ['ServiceChunk10']

#: 上游 `staleClaim = now - 5 * Time.minute`：`injecting` 超过 5 分钟视为可重处理的陈旧 claim。
_STALE_CLAIM_MS = 5 * 60 * 1000
#: 上游排水只处理 `occursAt <= now` 的行；去重视野回看 14 天。
_DEDUPE_WINDOW_MS = 14 * 24 * 60 * 60 * 1000
#: 上游 `Math.random() < 0.25`：整轮跳过的概率。
_SWEEP_SKIP_CHANCE = 0.25
#: 进 payload 的最近生活摘录：14 条 × 240 字；自身 world-event 产出被排除（反馈环切断）。
_RECENT_LIFE_LIMIT = 14
_RECENT_LIFE_CHARS = 240
_RECENT_SEEDED_LIMIT = 10


class ServiceChunk10(ServiceBase):
    """Chunk10 mixin：世界事件播种器（rc23）与其到点注入（rc26/rc28）。"""

    # ------------------------------------------------------------------ #
    # 运行期配置与可用性（上游 service.ts:780-790）
    # ------------------------------------------------------------------ #

    def world_seeder_runtime(self) -> dict[str, Any]:
        """上游 `this.worldSeederRuntime`：连接行勾选 + 配置夹取的合成结果。

        **总开关与勾选是 AND**：没勾「用于世界播种」的连接时 `enabled` 恒为 False，
        播种器整体关闭（零成本、零模型调用）。取**第一个**匹配的连接（上游用 `find`，
        不是连接池轮转）。
        """
        section = pick(self.config, 'world_seeder', 'worldSeeder') or {}
        provider = None
        for candidate in (pick(self.config, 'model', 'model_center') or {}).get('providers') or []:
            if not isinstance(candidate, dict):
                continue
            if candidate.get('enabled') is False:
                continue
            if candidate.get('use_for_world_seeding') is True and str(candidate.get('model') or '').strip():
                provider = candidate
                break
        return resolve_world_seeder_runtime(section, provider)

    def world_seeder_available(self) -> bool:
        """上游 `this.worldSeeder.available`：有没有可用的侧端连接。

        与 `world_seeder_runtime()['enabled']` 的区别：后者还要求总开关打开。
        """
        runtime = self.world_seeder_runtime()
        return bool(runtime.get('provider'))

    def explain_world_seeder_state(self) -> str:
        """启动自检用的可见结论（上游在这条路径上是静默的，见 PORTING_NOTES §31）。"""
        runtime = self.world_seeder_runtime()
        if runtime.get('enabled'):
            return '世界播种器已启用 间隔=%d分钟' % int(runtime.get('cadence_minutes') or 0)
        if runtime.get('provider'):
            return '世界播种器未启用：总开关关闭（模型已勾选「用于世界播种」）'
        return '世界播种器未启用：没有连接勾选「用于世界播种」'

    # ------------------------------------------------------------------ #
    # 生成（上游 worldSeederSweep）
    # ------------------------------------------------------------------ #

    async def world_seeder_sweep(self) -> None:
        """上游 `worldSeederSweep()`：一次后台生成检查。

        只对**唯一共享主剧本**生效（上游不是 per-story 遍历）。门序与上游一致，
        预检频控放在**模型调用之前**——省 token 的关键一手。
        """
        if getattr(self, 'desktop_runtime_phase', 'running') == 'paused':
            return
        if getattr(self, 'database_resetting', False):
            return
        runtime = self.world_seeder_runtime()
        if not runtime.get('enabled'):
            return
        if getattr(self, '_world_seeder_sweep_running', False):
            return
        self._world_seeder_sweep_running = True
        try:
            story = await self.get_canonical_story()  # type: ignore[attr-defined]
            if not story or not self.can_handle_story(story):  # type: ignore[attr-defined]
                return
            story_id = pick(story, 'id')
            generation = self.task_generation(story_id)
            # 上游：25% 概率整轮跳过（不是设计文档说的"间隔抖动"）。
            if random.random() < _SWEEP_SKIP_CHANCE:
                return
            now = self.now()
            rows = await self._seeded_event_rows(story_id)
            scheduled = [row for row in rows if pick(row, 'status') == STATUS_SCHEDULED]
            last24 = [
                row for row in rows
                if pick(row, 'status') == STATUS_INJECTED
                and (parse_dt(pick(row, 'updatedAt', 'updated_at')) is not None)
                and dt_ms(now) - dt_ms(parse_dt(pick(row, 'updatedAt', 'updated_at'))) < 24 * 60 * 60 * 1000
            ]
            if len(scheduled) >= int(runtime['max_pending']) or len(last24) >= int(runtime['daily_cap']):
                self.report_operation(  # type: ignore[attr-defined]
                    'diagnostic', 'debug', story, 'advance',
                    '世界播种器本轮跳过：频控命中 挂起=%d/%d 近24小时=%d/%d',
                    len(scheduled), runtime['max_pending'], len(last24), runtime['daily_cap'],
                )
                return
            domain = seed_domain_for_run(story_id, now, int(runtime['cadence_minutes']))
            system, user = await self._build_world_seeder_payload(story, now, rows, domain, runtime)
            try:
                raw = await self.narrator.generate_world_seeds(system, user, runtime)  # type: ignore[attr-defined]
            except Exception:
                health = getattr(self, 'health', None)
                if health is not None:
                    health.record_side_task(story_id, False)
                raise
            health = getattr(self, 'health', None)
            if health is not None:
                health.record_side_task(story_id, True)
            if not self.task_generation_current(story_id, generation):
                return
            drafts = parse_world_seed_events(raw)
            if not drafts:
                self.report_operation(  # type: ignore[attr-defined]
                    'diagnostic', 'debug', story, 'advance', '世界播种器本轮无事件',
                )
                return
            await self._persist_world_seed_drafts(
                story, now, drafts, rows, scheduled, last24, runtime,
            )
        except Exception as error:  # noqa: BLE001 - 后台任务失败绝不外抛
            self.report_standalone('warn', '世界播种器运行失败 错误=%s', error)  # type: ignore[attr-defined]
        finally:
            self._world_seeder_sweep_running = False

    async def _persist_world_seed_drafts(
        self,
        story: Any,
        now: datetime,
        drafts: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        scheduled: list[dict[str, Any]],
        last24: list[dict[str, Any]],
        runtime: dict[str, Any],
    ) -> None:
        """逐条走频控 + 校验闸后入库（上游 `worldSeederSweep` 的尾段）。"""
        story_id = pick(story, 'id')
        pending_count = len(scheduled)
        daily_count = len(last24)
        high_today = sum(1 for row in last24 if pick(row, 'importance') == 'high')
        recent_summaries = [str(pick(row, 'summary') or '') for row in rows if pick(row, 'summary')]
        blocked = self._world_seeder_blocked_names(story_id)
        story_state = pick(story, 'state') or {}
        timezone = str(pick(pick(story, 'setting') or {}, 'timezone') or 'Asia/Shanghai')
        for draft in drafts:
            if pending_count >= int(runtime['max_pending']) or daily_count >= int(runtime['daily_cap']):
                break
            if draft.get('importance') == 'high' and high_today >= 1:
                # 高重要性每天至多 1 条；`continue` 而不是 `break`——低/中照收。
                continue
            rejection = validate_seed_event(draft, {
                'now': now, 'timezone': timezone,
                'max_horizon_hours': runtime['max_horizon_hours'],
                'blocked_names': blocked, 'recent_summaries': recent_summaries,
            })
            if rejection:
                # 上游这里是 `diagnostic`（默认看不见）；移植版提到 standard，见 PORTING_NOTES。
                self.report_operation(  # type: ignore[attr-defined]
                    'standard', 'info', story, 'advance',
                    '世界事件被校验闸拒绝 原因=%s 摘要=%s', rejection, str(draft.get('summary'))[:80],
                )
                continue
            try:
                await self.db_create('interlude_seeded_event', {  # type: ignore[attr-defined]
                    'storyId': story_id,
                    'summary': draft['summary'],
                    'importance': draft['importance'],
                    'occursAt': draft['occursAt'],
                    'expiresAt': draft.get('expiresAt'),
                    'status': STATUS_SCHEDULED,
                    'subjects': draft.get('subjects') or [],
                    'sourcePayload': {'rationale': draft.get('rationale') or ''},
                    'createdAt': now, 'updatedAt': now,
                })
            except Exception as error:  # noqa: BLE001 - 一条失败不影响其余
                self.report_standalone('warn', '世界事件入库失败 错误=%s', error)  # type: ignore[attr-defined]
                continue
            pending_count += 1
            # 受控偏离：上游注释承诺"同批次内已被接受的草稿立即入列"，但代码里漏了
            # 这一步（一次性输出两条换皮时会双双入库）。这里补上。
            recent_summaries.append(str(draft['summary']))
            if draft.get('importance') == 'high':
                high_today += 1

    # ------------------------------------------------------------------ #
    # 到点注入（上游 drainDueSeededEvents，rc26 的 claim 状态机）
    # ------------------------------------------------------------------ #

    async def drain_due_seeded_events(self, story: Any, now: datetime, include_low: bool) -> None:
        """上游 `drainDueSeededEvents(story, now, includeLow)`。

        `include_low` 由相位决定（`phase != 'user-message'`）：**low 只在推进类回合排水**，
        免得一条无关紧要的背景事件劫持对话；medium/high 任何回合都排。被跳过的 low
        不丢——它留在 `scheduled`，下一个非 user-message 回合补排。
        """
        story_id = pick(story, 'id')
        stale_before = dt_ms(now) - _STALE_CLAIM_MS
        due: list[dict[str, Any]] = []
        for row in await self._seeded_event_rows(story_id):
            status = pick(row, 'status')
            occurs_at = parse_dt(pick(row, 'occursAt', 'occurs_at'))
            if occurs_at is None or dt_ms(occurs_at) > dt_ms(now):
                continue
            if status == STATUS_SCHEDULED:
                due.append(row)
                continue
            if status == STATUS_INJECTING:
                updated_at = parse_dt(pick(row, 'updatedAt', 'updated_at'))
                if updated_at is not None and dt_ms(updated_at) <= stale_before:
                    due.append(row)
        due.sort(key=lambda row: dt_ms(parse_dt(pick(row, 'occursAt', 'occurs_at'))))
        for row in due:
            expires_at = parse_dt(pick(row, 'expiresAt', 'expires_at'))
            if expires_at is not None and dt_ms(expires_at) <= dt_ms(now):
                await self.db_set(  # type: ignore[attr-defined]
                    'interlude_seeded_event', {'id': pick(row, 'id')},
                    {'status': STATUS_EXPIRED, 'updatedAt': now},
                )
                continue
            if pick(row, 'importance') == 'low' and not include_low:
                continue
            # 先 claim 再 append：两个并发排水不会为同一条事件生成两个剧本条目。
            await self.db_set(  # type: ignore[attr-defined]
                'interlude_seeded_event', {'id': pick(row, 'id')},
                {'status': STATUS_INJECTING, 'updatedAt': now},
            )
            try:
                entry = await self.append_entry(  # type: ignore[attr-defined]
                    story_id,
                    {
                        'kind': 'world-event', 'actor': 'system',
                        'content': '[世界事件] %s' % pick(row, 'summary'),
                        'occurredAt': iso(parse_dt(pick(row, 'occursAt', 'occurs_at'))),
                        'metadata': {
                            'seeded_event_id': pick(row, 'id'),
                            'seededEventId': pick(row, 'id'),
                            'importance': pick(row, 'importance'),
                        },
                    },
                    now,
                )
            except Exception as error:  # noqa: BLE001 - 回滚后抛错，由外层 warn 收口
                try:
                    await self.db_set(  # type: ignore[attr-defined]
                        'interlude_seeded_event', {'id': pick(row, 'id')},
                        {'status': STATUS_SCHEDULED, 'updatedAt': now},
                    )
                except Exception:  # noqa: BLE001 - 回滚失败不再掩盖原始错误
                    pass
                self.report('warn', story, 'advance', '世界事件排水失败（不阻断回合）')  # type: ignore[attr-defined]
                raise error
            await self.db_set(  # type: ignore[attr-defined]
                'interlude_seeded_event', {'id': pick(row, 'id')},
                {'status': STATUS_INJECTED, 'injectedEntryId': pick(entry, 'id'), 'updatedAt': now},
            )

    # ------------------------------------------------------------------ #
    # 数据与 payload
    # ------------------------------------------------------------------ #

    async def _seeded_event_rows(self, story_id: str) -> list[dict[str, Any]]:
        """该剧本的全部播种事件行。

        ⚠️ 上游用 `$gte` / `$lte` / `$or` 查询；我方 `db_get` 对算子**显式抛错**
        （见 PORTING_NOTES 的「范围算子查询退化」），因此这里取全表 + Python 侧过滤。
        事件表很小（挂起上限 4、每日上限 4），代价可忽略。
        """
        return await self.db_get('interlude_seeded_event', {'storyId': story_id})  # type: ignore[attr-defined]

    def _world_seeder_blocked_names(self, story_id: str) -> list[str]:
        """上游 `BLOCKED NAMES`：参与者 displayName（trim 后长度 ≥ 2）。"""
        names: list[str] = []
        for participant in getattr(self, '_world_seeder_participants', []) or []:
            name = str(pick(participant, 'displayName', 'display_name') or '').strip()
            if len(name) >= 2 and name not in names:
                names.append(name)
        return names

    async def _build_world_seeder_payload(
        self,
        story: Any,
        now: datetime,
        rows: list[dict[str, Any]],
        domain: dict[str, Any],
        runtime: dict[str, Any],
    ) -> tuple[str, str]:
        """上游 `buildWorldSeederPayload()` → `(system, user)` 两段文本。"""
        import json as _json

        story_id = pick(story, 'id')
        setting = pick(story, 'setting') or {}
        timezone = str(pick(setting, 'timezone') or 'Asia/Shanghai')
        self._world_seeder_participants = await self.participants(story_id)  # type: ignore[attr-defined]
        blocked = self._world_seeder_blocked_names(story_id)
        scheduled = [row for row in rows if pick(row, 'status') == STATUS_SCHEDULED]
        recent_injected = [
            row for row in rows
            if pick(row, 'status') == STATUS_INJECTED
            and parse_dt(pick(row, 'occursAt', 'occurs_at')) is not None
            and dt_ms(now) - dt_ms(parse_dt(pick(row, 'occursAt', 'occurs_at'))) <= _DEDUPE_WINDOW_MS
        ]
        window_rows = scheduled + recent_injected
        # 反馈环切断（rc28 核心修复）：生活摘录**排除自己产出的 world-event**，
        # 灵感来源必须是她本人的生活，自身产出只留在 recentlySeededEvents 清单里。
        entries = await self.db_get(  # type: ignore[attr-defined]
            'interlude_script_entry', {'storyId': story_id},
        )
        recent_life = []
        for entry in entries[-40:]:
            if pick(entry, 'kind') == 'world-event':
                continue
            content = str(pick(entry, 'content') or '').strip()
            if not content:
                continue
            recent_life.append({
                'kind': pick(entry, 'kind'),
                'at': iso(parse_dt(pick(entry, 'occurredAt', 'occurred_at'))),
                'text': content[:_RECENT_LIFE_CHARS],
            })
        recent_life = recent_life[-_RECENT_LIFE_LIMIT:]
        state = pick(story, 'state') or {}
        working_details = [
            {'label': pick(item, 'label'), 'value': pick(item, 'value')}
            for item in (pick(state, 'working_details', 'workingDetails') or [])[-8:]
        ]
        payload = {
            'nowLocal': now.astimezone().strftime('%A, %B %d, %Y at %H:%M'),
            'timezone': timezone,
            'season': season_for_month(now.month),
            'worldSetting': {
                'characterName': pick(pick(setting, 'character') or {}, 'name'),
                'characterProfile': str(pick(pick(setting, 'character') or {}, 'profile') or '')[:600],
                'world': str(pick(setting, 'world') or '')[:800],
                'location': str(pick(setting, 'location') or '')[:300],
                'supportingCast': str(pick(setting, 'supportingCast', 'supporting_cast') or '')[:400],
            },
            'currentScene': '',
            'currentArc': '',
            'recentEstablishedLife': recent_life,
            'workingDetails': working_details,
            'blockedNames': blocked,
            'thisRunSlice': domain,
            'recentlySeededEvents': [
                str(pick(row, 'summary') or '') for row in window_rows[-_RECENT_SEEDED_LIMIT:]
            ],
            'constraints': {
                'maxHorizonHours': runtime['max_horizon_hours'],
                'dailyBudget': runtime['daily_cap'],
                'note': 'BLOCKED NAMES are off-limits; offline channels only; most runs return empty events.',
            },
        }
        return world_seeder_system_prompt(domain), _json.dumps(payload, ensure_ascii=False)


# 说明：`asyncio` / `timedelta` / `Optional` 在下面几个方法里按需使用；
# 保留导入是为了与其它 chunk 的头部结构一致（避免 lint 噪音也便于后续扩展）。
_ = (asyncio, timedelta, Optional)
