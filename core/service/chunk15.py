"""Chunk15 mixin：长线叙事催化器（上游 `src/long-arc.ts` + `service.ts` 的挂载段）的接线层。

纯函数与数据形状在 `plugin/core/long_arc.py`（上游 `src/long-arc.ts` 的逐条移植，
含 13 条上游纯函数测试的对应物）；本文件是**宿主那一半**：

| 本文件的成员 | 干什么 | 上游对应 | 谁会调 |
| --- | --- | --- | --- |
| `long_horizon_section` / `long_horizon_config` / `long_horizon_enabled` | 读 `long_horizon` 配置段 | `service.ts:851` | 本文件全部入口 |
| `explain_long_horizon_state()` | 启动自检的一句话结论（"为什么没生效"） | ——（本移植版新增） | 适配层启动日志 |
| `long_horizon_progress()` / `save_long_horizon_progress()` | 累计器读写（每剧本一行） | `service.ts:8346/8351` | sweep |
| `get_active_long_arc_guidance()` | 取 active 指导；**过期即转 `expired`** | `service.ts:8360` | sweep / projection |
| `ensure_long_horizon_guidance_loaded()` | 每 story 只从库里加载一次 | `service.ts:8377` | 主叙事回合入口 |
| `long_horizon_prompt_projection(story_id)` | **主叙事注入点**：给主叙事的"长期走向"提示块 | `service.ts:8533` | 回合上下文组装 |
| `startup_preload_long_horizon_guidance()` | 启动期把 active 行灌进缓存 | `service.ts:853` | 适配层启动 |
| `long_horizon_sweep(story)` | 扫新条目 → 累计 → 判触发 | `service.ts:8298` | **剧本提交后的那条返回路径**（见 §89.3 补丁） |
| `schedule_long_horizon_sweep(story)` | 上面那个的**异步点火**版（异常就地吸收 + 可见 warn） | `service.ts:5218` 的 `void … .catch(…)` | 同上 |
| `long_horizon_generate(...)` | 调长线模型 → 归一化 → supersede + 写新版本 | `service.ts:8384` | sweep |
| `long_horizon_input(story, eligible)` | 组装分层模型输入 | `service.ts:8450` | generate |
| `drain_long_horizon_tasks()` / `stop_long_horizon()` | 收尾：等在飞任务 / 停止接收 | ——（本移植版新增，见 PORTING_NOTES §83） | 适配层 `terminate()`、测试 |

## 接线位置（本文件只提供**注入点**，调用点在别人的文件里）

`chunk1..14` / `narrator.py` / `narrator_prompts.py` / `chunk4.py` 都**没有被本文件改动**——
需要的三处调用写成逐字补丁放在 `docs/PORTING_NOTES.md` §89.3：

1. `plugin/core/service/__init__.py`：把 chunk15 混进 `InterludeService`（`range(1, 15)` → `range(1, 16)`）；
2. 剧本提交成功后的返回路径：`self.schedule_long_horizon_sweep(story)`；
3. 主叙事上下文组装：`await self.ensure_long_horizon_guidance_loaded(story_id)` +
   请求里带 `longHorizonGuidance`（投影插在 `CHANNELS` 行**之前**）。

## 与上游的受控偏离

1. **范围查询退化成"先取一窗再在 Python 侧过滤"**（PORTING_NOTES 的第 5 条受控偏离）：
   上游 `dbGet(..., {id: {$gt: lastEntryId}}, {sort: {id: 'asc'}, limit: 500})` 在本移植版
   必须改写。这里用 `sort: {id: 'asc'}, limit: 500` 取**最旧的一窗**再过滤 `id > 游标`：
   游标只会单调前进，宁可分多轮扫完，绝不跳过未计入的条目（`limit` 方向反了会静默丢证据）。
2. **生成是 `await` 的，不是 detached promise**：上游 `void this.longHorizonGenerate(...)`
   把生成甩出去、sweep 立刻返回。本移植版把"异步"挪到调用侧（`schedule_long_horizon_sweep`），
   这样异常能被捕获、行为可测，且同一 story 不会并发生成（`generating` 集合照旧守着）。
3. **`db_set` 不写主键列**：`interlude_long_arc_progress` 的主键是 `storyId`，
   上游 minato 拒绝带主键的 update——本移植版把 `storyId` 从 patch 里摘掉再写
   （同 `save_schedule_preplan` 的处理，PORTING_NOTES 第 6 条）。
4. **模型入口缺失要看得见**：上游 `this.compactor.planLongArcGuidance?.()` 用可选链静默跳过；
   本移植版打一条 warn（"指名了却没有入口"必须能排障，坑 25/45/48）。
5. **`long_horizon_prompt_projection` 是同步的**：上游读进程内缓存；这里同形
   （先 `ensure_long_horizon_guidance_loaded` 再投影），不把它改成每回合查库。
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

from ..long_arc import (
    DEFAULT_LONG_HORIZON_CONFIG,
    GUIDANCE_TABLE,
    PROGRESS_TABLE,
    build_long_horizon_input,
    calculate_long_horizon_score,
    is_eligible_narrative_entry,
    long_horizon_prompt_projection,
    merge_long_horizon_progress,
    normalize_long_arc_decision,
    resolve_long_horizon_config,
    should_trigger_long_horizon,
)
from ..story_state import decode_story_state
from ..time import parse_dt
from .base import ServiceBase, participant_id_for_story, pick

__all__ = ['ServiceChunk15', 'LONG_HORIZON_SCAN_LIMIT']

#: 单次扫描读取的剧本条目窗口（上游 `service.ts:8303` 的 `limit: 500`）。
LONG_HORIZON_SCAN_LIMIT = 500

#: 表名（与 `core/database.py` 的两张表一致，判据一处）。
GUIDANCE = GUIDANCE_TABLE
PROGRESS = PROGRESS_TABLE


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int_of(value: Any, *names: str) -> int:
    """读一个非负整数（双拼写）；读不出来回 0。"""
    for name in names:
        raw = pick(value, name) if isinstance(value, Mapping) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        return int(raw)
    return 0


class ServiceChunk15(ServiceBase):
    """Chunk15：长线叙事催化器的接线层（配置门 → 累计器 → 生成 → 主叙事投影）。"""

    # ------------------------------------------------------------------ #
    # 实例状态（每实例一份；**不要**写成类属性，测试之间会串）
    # ------------------------------------------------------------------ #

    def _long_horizon_state(self) -> dict[str, Any]:
        state = self.__dict__.get('_long_horizon_runtime')
        if state is None:
            state = {
                #: storyId → 当前 active 指导行（上游 `activeLongArcGuidanceCache`）。
                'cache': {},
                #: 正在扫描 / 正在生成 / 已经加载过 active 的 story（上游三个 Set）。
                'sweep_running': set(),
                'generating': set(),
                'loaded': set(),
                #: storyId → 最近一次累计总分（上游 `longHorizonLastScore`）。
                'last_score': {},
                #: `schedule_long_horizon_sweep` 甩出去的在飞任务（收尾用）。
                'tasks': set(),
            }
            self.__dict__['_long_horizon_runtime'] = state
        return state

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #

    def long_horizon_section(self) -> dict[str, Any]:
        """读 `long_horizon` 配置段（宿主 bridge 的 `section()` 优先，回落 `self.config`）。"""
        reader = getattr(self, 'section', None)
        if callable(reader):
            try:
                data = reader('long_horizon')
            except Exception:  # noqa: BLE001 - 旧配置没有这个分组是正常的
                data = None
            if isinstance(data, Mapping) and data:
                return dict(data)
        return _mapping(pick(self.config, 'long_horizon'))

    def long_horizon_config(self) -> dict[str, Any]:
        """归一化后的 `long_horizon` 配置（判据只有 `core/long_arc.resolve_long_horizon_config` 一处）。"""
        return resolve_long_horizon_config(self.long_horizon_section())

    def long_horizon_enabled(self) -> bool:
        """长线叙事催化器是否启用（面向模型/库的入口都先过这道门）。"""
        return bool(self.long_horizon_config()['enabled'])

    def explain_long_horizon_state(self) -> str:
        """启动自检用的一句话结论（"为什么不生效"）。"""
        config = self.long_horizon_config()
        if not config['enabled']:
            return '长线叙事催化器未启用（配置 long_horizon.enabled 为 false 或缺失）'
        return '长线叙事催化器已启用：首次门槛=%d 复审增量=%d 强度=%s' % (
            config['trigger_score'], config['review_increment'], config['intensity'],
        )

    # ------------------------------------------------------------------ #
    # 存储：累计器（每剧本一行）
    # ------------------------------------------------------------------ #

    async def long_horizon_progress(self, story_id: Any) -> Optional[dict[str, Any]]:
        """读累计器行（上游 `longHorizonProgress`，`service.ts:8346`）。"""
        rows = await self.db_get(PROGRESS, {'storyId': story_id}, {'limit': 1})
        return rows[0] if rows else None

    async def save_long_horizon_progress(self, progress: Mapping[str, Any]) -> None:
        """写累计器行（上游 `saveLongHorizonProgress`，`service.ts:8351`）。

        `storyId` 是主键：**patch 里必须摘掉它**（`db.set` 剥主键列，留着是 no-op；
        这里显式摘掉，语义与上游 minato 的"update 不接受主键"一致）。
        """
        row = _mapping(progress)
        story_id = row.get('storyId')
        existing = await self.long_horizon_progress(story_id)
        patch = {key: value for key, value in row.items() if key != 'storyId'}
        if existing:
            await self.db_set(PROGRESS, {'storyId': story_id}, patch)
        else:
            await self.db_create(PROGRESS, row)

    # ------------------------------------------------------------------ #
    # 存储：指导行（版本链）
    # ------------------------------------------------------------------ #

    async def get_active_long_arc_guidance(self, story_id: Any) -> Optional[dict[str, Any]]:
        """取 active 指导（上游 `getActiveLongArcGuidance`，`service.ts:8360`）。

        过期是**生命周期转移**，不是投影期过滤：过期行必须就地转 `expired`，
        否则它会永远挡住下一次 `first-trigger`（只过滤不落库会复现上游那个 bug）。
        """
        rows = await self.db_get(
            GUIDANCE, {'storyId': story_id, 'status': 'active'},
            {'sort': {'version': 'desc'}, 'limit': 1},
        )
        active = rows[0] if rows else None
        if not active:
            return None
        expires_at = parse_dt(active.get('expiresAt'))
        if expires_at is not None and expires_at <= self.now():
            if active.get('id') is not None:
                await self.db_set(
                    GUIDANCE, {'id': active.get('id')},
                    {'status': 'expired', 'updatedAt': self.now()},
                )
            self._long_horizon_state()['cache'].pop(story_id, None)
            return None
        return active

    async def ensure_long_horizon_guidance_loaded(self, story_id: Any) -> Optional[dict[str, Any]]:
        """每个 story 只从库里加载一次 active 指导（上游 `ensureLongHorizonGuidanceLoaded`）。"""
        state = self._long_horizon_state()
        if not self.long_horizon_enabled() or story_id in state['loaded']:
            return state['cache'].get(story_id)
        active = await self.get_active_long_arc_guidance(story_id)
        if active:
            state['cache'][story_id] = active
        state['loaded'].add(story_id)
        return active

    async def startup_preload_long_horizon_guidance(self) -> Optional[dict[str, Any]]:
        """启动期把主剧本的 active 行灌进缓存（上游 `service.ts:853`）。

        失败/没有主剧本都不抛：预热是可选的，缺了会在回合入口补上。
        """
        if not self.long_horizon_enabled():
            return None
        story = await self.get_canonical_story()
        if not story:
            return None
        return await self.ensure_long_horizon_guidance_loaded(pick(story, 'id'))

    def long_horizon_prompt_projection(self, story_id: Any) -> Optional[str]:
        """**主叙事注入点**（上游 `longHorizonPromptProjection`，`service.ts:8533`）。

        返回给主叙事的"长期走向"提示块（软许可，不是剧本）；没有 active / 已过期 /
        找不到阶段时回 `None`。**同步**：读的是进程内缓存，所以调用方必须先
        `await ensure_long_horizon_guidance_loaded(story_id)`（上游同形）。
        """
        if not self.long_horizon_enabled():
            return None
        active = self._long_horizon_state()['cache'].get(story_id)
        if not active:
            return None
        return long_horizon_prompt_projection(active, self.now())

    # ------------------------------------------------------------------ #
    # 扫描与判定
    # ------------------------------------------------------------------ #

    async def long_horizon_unscanned_entries(self, story_id: Any, last_entry_id: int) -> list[Any]:
        """取游标之后**尚未计入**的剧本条目（上游 `service.ts:8302`）。

        上游用 `id: {$gt: lastEntryId}`；本移植版的 `db_get` 对算子显式报错，所以取最旧的
        一窗再在 Python 侧过滤。**方向不能反**：取最新一窗会让游标跳过中间的条目。
        """
        rows = await self.db_get(
            'interlude_script_entry', {'storyId': story_id},
            {'sort': {'id': 'asc'}, 'limit': LONG_HORIZON_SCAN_LIMIT},
        )
        return [row for row in rows if _int_of(row, 'id') > last_entry_id]

    async def long_horizon_sweep(self, story: Any, now: Any = None) -> dict[str, Any]:
        """上游 `longHorizonSweep(story, now)`（`service.ts:8298`）逐条移植。

        扫新条目 → 按权重累计 → 判是否该审查 → 该审查就调长线模型。**始终不抛**给调用方
        （上游在提交路径上用浮动 promise + `.catch` 吸收；见 `schedule_long_horizon_sweep`）。
        """
        config = self.long_horizon_config()
        if not config['enabled']:
            return {'triggered': False, 'reason': 'disabled', 'generated': False, 'total_score': 0.0}
        story_id = pick(story, 'id')
        state = self._long_horizon_state()
        if story_id in state['sweep_running'] or story_id in state['generating']:
            return {'triggered': False, 'reason': 'busy', 'generated': False, 'total_score': 0.0}
        state['sweep_running'].add(story_id)
        try:
            progress = await self.long_horizon_progress(story_id)
            last_entry_id = _int_of(progress, 'lastCountedEntryId')
            entries = await self.long_horizon_unscanned_entries(story_id, last_entry_id)
            if not entries:
                return {
                    'triggered': False, 'reason': 'no-entries', 'generated': False,
                    'total_score': float(_int_of(progress, 'totalScore')),
                }

            score = calculate_long_horizon_score(entries, config)
            last_scanned = max([_int_of(entry, 'id') for entry in entries] + [last_entry_id])
            moment = parse_dt(now) or self.now()
            cumulative = merge_long_horizon_progress(
                {**_mapping(progress), 'storyId': story_id}, score, last_scanned, moment,
            )
            await self.save_long_horizon_progress(cumulative)
            total = float(cumulative['totalScore'])
            state['last_score'][story_id] = total

            active = await self.get_active_long_arc_guidance(story_id)
            trigger = should_trigger_long_horizon(
                {'total_score': total}, active,
                cumulative.get('lastGenerationScore'), config,
            )
            if not trigger['trigger']:
                return {'triggered': False, 'reason': trigger['reason'], 'generated': False, 'total_score': total}
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '长线指导触发 原因=%s 总分=%.1f 私聊=%.1f 群聊=%.1f',
                trigger['reason'], total,
                float(cumulative.get('privateScore') or 0.0),
                float(cumulative.get('groupScore') or 0.0),
            )
            state['generating'].add(story_id)
            try:
                created = await self.long_horizon_generate(story, entries, total, cumulative)
            finally:
                state['generating'].discard(story_id)
            return {
                'triggered': True, 'reason': trigger['reason'],
                'generated': created is not None, 'total_score': total,
            }
        finally:
            state['sweep_running'].discard(story_id)

    def schedule_long_horizon_sweep(self, story: Any, now: Any = None) -> None:
        """把扫描甩到后台（上游 `void this.longHorizonSweep(story, now).catch(...)`）。

        **不阻塞主回合**，但异常必须就地吸收并打可见 warn（浮动任务逃逸会变成
        unhandledRejection / "日志里什么都没有"）。
        """
        if not self.long_horizon_enabled():
            return
        state = self._long_horizon_state()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 没有事件循环时只能同步放弃
            self.report_standalone('warn', '长线扫描未能排入后台：当前没有事件循环')
            return
        task = loop.create_task(self.long_horizon_sweep(story, now))
        state['tasks'].add(task)

        def _done(finished: Any) -> None:
            state['tasks'].discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                self.report_standalone('warn', '长线扫描失败，保留既有剧本与投递 错误=%s', error)

        task.add_done_callback(_done)

    async def drain_long_horizon_tasks(self) -> None:
        """等所有在飞的后台扫描跑完（测试与收尾用；见 PORTING_NOTES §83）。"""
        tasks = [task for task in self._long_horizon_state()['tasks'] if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop_long_horizon(self) -> None:
        """停止接收新的后台扫描（在飞任务留待 `drain_long_horizon_tasks` 收）。"""
        for task in list(self._long_horizon_state()['tasks']):
            task.cancel()

    # ------------------------------------------------------------------ #
    # 生成
    # ------------------------------------------------------------------ #

    async def long_horizon_input(self, story: Any, eligible: list[Any]) -> dict[str, Any]:
        """组装发给长线模型的分层输入（上游 `longHorizonInput`，`service.ts:8450`）。"""
        story_id = pick(story, 'id')
        state = decode_story_state(pick(story, 'state'))
        overlay = pick(state, 'setting_overlay', 'settingOverlay')
        participant_id = participant_id_for_story(
            story_id, pick(story, 'platform'), pick(story, 'selfId', 'self_id'),
            pick(story, 'userId', 'user_id'),
        )
        recent, historical, arcs, progress, participants, facts, active = await asyncio.gather(
            self.recent_entries_for_prompt(story_id, self.now()),
            self.db_get('interlude_script_entry', {'storyId': story_id}, {'sort': {'id': 'desc'}, 'limit': 120}),
            self.db_get('interlude_arc', {'storyId': story_id, 'status': 'active'}),
            self.long_horizon_progress(story_id),
            self.db_get(
                'interlude_participant', {'storyId': story_id, 'status': 'active'},
                {'limit': 12, 'sort': {'updatedAt': 'desc'}},
            ),
            self.db_get(
                'interlude_fact', {'storyId': story_id, 'status': 'active'},
                {'limit': 24, 'sort': {'importance': 'desc', 'updatedAt': 'desc'}},
            ),
            self.get_active_long_arc_guidance(story_id),
        )
        config = self.long_horizon_config()
        return build_long_horizon_input(
            story,
            recent_entries=recent,
            historical_entries=historical,
            arcs=arcs,
            participants=participants,
            facts=facts,
            active=active,
            progress=progress,
            eligible=eligible,
            overlay=overlay,
            primary_participant_id=participant_id,
            share_participant_details=bool(pick(
                self.shared_story_config, 'shareParticipantDetails', 'share_participant_details',
            )),
            intensity=config['intensity'],
            last_score=self._long_horizon_state()['last_score'].get(story_id),
        )

    async def long_horizon_generate(
        self,
        story: Any,
        evidence: list[Any],
        current_score: float,
        progress: Mapping[str, Any],
    ) -> Optional[dict[str, Any]]:
        """上游 `longHorizonGenerate(story, evidence, currentScore, progress)`（`service.ts:8384`）。

        返回写入的 payload（没写入回 `None`）。**隐私边界只作用于模型输入，不作用于计分**
        （上游 `service.ts:8396`）：`share_participant_details=false` 时别人的条目进不了模型
        上下文，但累计分数照旧全额累计。
        """
        story_id = pick(story, 'id')
        config = self.long_horizon_config()
        share_details = bool(pick(
            self.shared_story_config, 'shareParticipantDetails', 'share_participant_details',
        ))
        participant_id = participant_id_for_story(
            story_id, pick(story, 'platform'), pick(story, 'selfId', 'self_id'),
            pick(story, 'userId', 'user_id'),
        )

        def visible(value: Any) -> bool:
            if share_details:
                return True
            owner = pick(value, 'participantId', 'participant_id')
            return not owner or owner == participant_id

        eligible = [entry for entry in evidence if is_eligible_narrative_entry(entry) and visible(entry)]
        if not eligible:
            return None

        model_input = await self.long_horizon_input(story, eligible)
        historical_ids = [
            _int_of(item, 'id') for item in _as_list(model_input.get('historicalEvidence'))
        ]
        valid_ids = set(_int_of(entry, 'id') for entry in eligible) | set(historical_ids)

        planner = getattr(self.compactor, 'plan_long_arc_guidance', None)
        if not callable(planner):
            self.report_operation(
                'standard', 'warn', story, 'advance',
                '长线指导已触发，但模型入口未接线（plan_long_arc_guidance 缺失），本轮不生成，保留既有指导',
            )
            return None
        output = await planner(model_input)
        if not output:
            return None

        result = normalize_long_arc_decision(output, valid_ids, config)
        if result is None:
            self.report_standalone_operation(
                'diagnostic', 'warn', '长线指导输出被归一化拒绝（决策/证据引用/催化结构不合法）',
            )
            return None

        trigger_entry_id = _int_of(eligible[-1], 'id')
        # 休眠是合法的"不写入"结论：它推进复审基线，但**不碰**已经可用的 active 催化。
        if result['decision'] == 'dormant' or not result.get('payload'):
            await self.save_long_horizon_progress({
                **_mapping(progress), 'storyId': story_id,
                'totalScore': current_score, 'lastGenerationScore': current_score,
                'lastGenerationEntryId': trigger_entry_id, 'updatedAt': self.now(),
            })
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '长线指导本轮保持休眠：%s',
                result.get('reason') or '当前没有适合播种的长期发展可能性',
            )
            return None

        payload = result['payload']
        active = await self.get_active_long_arc_guidance(story_id)
        if active is not None and active.get('id') is not None:
            await self.db_set(
                GUIDANCE, {'id': active.get('id')},
                {'status': 'superseded', 'updatedAt': self.now()},
            )
        latest = await self.db_get(GUIDANCE, {'storyId': story_id}, {'sort': {'version': 'desc'}, 'limit': 1})
        # 版本链：上游 `Number(latestRows[0]?.version ?? active?.version ?? 0) + 1`——
        # 最新的那行缺席时回落到 active 的版本，再不行就是 0（首个版本 = 1）。
        latest_version = _int_of(latest[0], 'version') if latest else 0
        active_version = _int_of(active, 'version') if isinstance(active, Mapping) else 0
        next_version = max(latest_version, active_version) + 1
        moment = self.now()
        row: dict[str, Any] = {
            'storyId': story_id,
            'version': next_version,
            'status': 'active',
            'title': payload['title'],
            'premise': payload['premise'],
            'direction': payload['direction'],
            'payload': payload,
            'currentStage': pick(payload.get('currentStage'), 'id'),
            'intensity': payload['intensity'],
            'confidence': payload['confidence'],
            'triggerEntryId': trigger_entry_id,
            'evidenceEntryIds': payload['evidenceEntryIds'],
            'createdAt': moment,
            'updatedAt': moment,
        }
        if active is not None and active.get('id') is not None:
            row['supersedesId'] = active.get('id')
        await self.db_create(GUIDANCE, row)
        await self.save_long_horizon_progress({
            **_mapping(progress), 'storyId': story_id,
            'totalScore': current_score, 'lastGenerationScore': current_score,
            'lastGenerationEntryId': trigger_entry_id, 'updatedAt': moment,
        })
        created = await self.get_active_long_arc_guidance(story_id)
        if created:
            self._long_horizon_state()['cache'][story_id] = created
        stage = pick(payload.get('currentStage'), 'name')
        self.report_operation(
            'standard', 'info', story, 'advance',
            '长线指导已生成 版本=%d 决策=%s 阶段=%s 标题=%s 置信度=%.2f',
            next_version, result['decision'], stage, payload['title'], payload['confidence'],
        )
        return payload


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


#: 缺省配置（供适配层读取默认值时对账；判据仍在 `core/long_arc.py`）。
LONG_HORIZON_DEFAULTS = DEFAULT_LONG_HORIZON_CONFIG
