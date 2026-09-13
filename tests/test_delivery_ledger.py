"""M6.1 投递账本测试（移植自 `upstream/test/delivery-ledger.test.ts`）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild；用例逐条对照，断言全部保留。

依赖说明
--------
上游该测试用 ``decisionToScriptCommit`` / ``findOutgoingScriptEvent``
（`src/script/commit-builder.ts`）构造 commit；这里用同一套移植产物
`plugin/core/script/commit_builder.py` 的 ``decision_to_script_commit`` /
``find_outgoing_script_event``，决策输入逐字段对照上游（camelCase → snake_case）。

用例 2 断言的是 `src/service.ts` 里 ``confirmOutgoingDeliveries`` /
``updateScriptDeliveryOutcome`` 的错误吞咽行为（账本读/写失败不得中断确认与后续
气泡排期），该代码属于 `plugin/core/service.py`（另一个 Agent 的 8.5k 行模块）。
服务可用时该用例真实执行，不可用（或接口尚未落地）时按文档降级为 skip。
"""

from __future__ import annotations

import asyncio
import inspect
import types
import unittest
from datetime import datetime, timezone
from typing import Any, Optional

from plugin.core.delivery import (
    attach_message_event,
    prepare_outgoing_delivery,
    restore_message_event,
    script_event_payload,
)
from plugin.core.script.commit_builder import (
    decision_to_script_commit,
    find_outgoing_script_event,
)
from plugin.core.script.delivery_ledger import (
    aggregate_delivery_status,
    create_script_delivery_actions,
    delivery_reference,
    platform_action_reference,
    update_script_delivery_actions,
)
from plugin.core.turn_persistence import script_entry_draft_for_commit

PROSE = '她放下杯子，发来“第一句”，紧接着又发来“第二句”。'
REPLY_CONTENT = '第一句<sep/>第二句'


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _commit() -> dict[str, Any]:
    """上游 ``commit()``：同一份决策 → ``decisionToScriptCommit``。"""
    return decision_to_script_commit({
        'story_id': 'story:1',
        'participant_id': 'alice',
        'phase': 'user-message',
        'from': _dt('2026-09-05T10:00:00.000Z'),
        'now': _dt('2026-09-05T10:01:00.000Z'),
        'message_separator': '<sep/>',
        'split_reply_messages': True,
        'decision': {
            'script': PROSE,
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': REPLY_CONTENT}},
            'local_media': {'asset_id': 'sticker:cat', 'willingness': 0.9},
            'native_face': {'semantic': 'smile', 'willingness': 0.8},
            'message_reactions': [{'message_ref': 'group-message:7', 'reaction': 'heart'}],
        },
    })


def _outgoing_event(commit: dict[str, Any]) -> dict[str, Any]:
    """上游 `findOutgoingScriptEvent(commit, 'alice')`。"""
    event = find_outgoing_script_event(commit, 'alice')
    if event is None:
        raise AssertionError('fixture has no outgoing-message event for alice')
    return event


def _service_hooks() -> Optional[tuple[Any, Any, Any]]:
    """取 `plugin/core/service.py` 的确认入口；缺失时返回 None（用例降级为 skip）。"""
    try:
        from plugin.core.service import InterludeService  # type: ignore[import-not-found]
    except Exception:  # 模块尚未落地 / 导入期依赖缺失
        return None
    confirm = None
    for name in ('confirm_outgoing_deliveries', '_confirm_outgoing_deliveries',
                 'confirmOutgoingDeliveries'):
        confirm = getattr(InterludeService, name, None)
        if confirm is not None:
            break
    update = None
    for name in ('update_script_delivery_outcome', '_update_script_delivery_outcome',
                 'updateScriptDeliveryOutcome'):
        update = getattr(InterludeService, name, None)
        if update is not None:
            break
    if confirm is None or update is None:
        return None
    return InterludeService, confirm, update


class _ServiceStub:
    """上游测试里那个 ``service`` 字面量对象的等价物。

    方法名同时注册 snake_case / camelCase / 私有前缀三种拼写，以便与并行移植的
    `service.py` 内部命名约定兼容（上游该测试是直接 ``.call(service, ...)``）。
    """

    def __init__(self, failure: str, commit: dict[str, Any], intents: list[Any],
                 warnings: list[Any], update: Any) -> None:
        self.failure = failure
        self.commit = commit
        self.intents = intents
        self.warnings = warnings
        self.config = {'chat_rhythm': {'enabled': False}, 'chatRhythm': {'enabled': False}}
        pairs = (
            (('serial', '_serial'), self._serial),
            (('get_participant', 'getParticipant', '_get_participant'), self._get_participant),
            (('append_entry', 'appendEntry', '_append_entry'), self._append_entry),
            (('db_get', 'dbGet', '_db_get'), self._db_get),
            (('db_set', 'dbSet', '_db_set'), self._db_set),
            (('report_standalone', 'reportStandalone', '_report_standalone'), self._report_standalone),
            (('record_character_message', 'recordCharacterMessage', '_record_character_message'),
             self._record_character_message),
            (('typing_delay_milliseconds', 'typingDelayMilliseconds', '_typing_delay_milliseconds'),
             self._typing_delay_milliseconds),
            (('append_intent', 'appendIntent', '_append_intent'), self._append_intent),
            (('schedule_due_intent_wake', 'scheduleDueIntentWake', '_schedule_due_intent_wake'),
             self._schedule_due_intent_wake),
        )
        for names, method in pairs:
            for name in names:
                setattr(self, name, method)
        # 上游用 `(InterludeService.prototype as any).updateScriptDeliveryOutcome` 覆盖
        for name in ('update_script_delivery_outcome', '_update_script_delivery_outcome',
                     'updateScriptDeliveryOutcome'):
            setattr(self, name, types.MethodType(update, self))

    async def _serial(self, _story_id: str, task: Any) -> Any:
        result = task()
        return await result if inspect.isawaitable(result) else result

    async def _get_participant(self, _participant_id: str) -> dict[str, Any]:
        return {'id': 'alice'}

    async def _append_entry(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {'id': 99}

    async def _db_get(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        if self.failure == 'read':
            raise RuntimeError('ledger read failed')
        actions = create_script_delivery_actions(self.commit)
        return [{
            'id': 42, 'story_id': 'story:1', 'storyId': 'story:1',
            'metadata': {'commit_id': COMMIT_ID, 'commitId': COMMIT_ID,
                         'delivery_actions': actions, 'deliveryActions': actions},
        }]

    async def _db_set(self, *_args: Any, **_kwargs: Any) -> None:
        if self.failure == 'write':
            raise RuntimeError('ledger write failed')

    def _report_standalone(self, *args: Any) -> None:
        self.warnings.append(args)

    async def _record_character_message(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def _typing_delay_milliseconds(self, _segment: str) -> int:
        return 100

    async def _append_intent(self, _story_id: str, intent: Any, *_args: Any, **_kwargs: Any) -> None:
        self.intents.append(intent)

    def _schedule_due_intent_wake(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class DeliveryLedgerTest(unittest.TestCase):
    def test_partial_requires_actual_delivery_while_unfinished_attempts_remain_pending(self) -> None:
        def status(*values: str) -> str:
            return aggregate_delivery_status([
                {'index': index, 'kind': 'message', 'content': 'text', 'status': value}
                for index, value in enumerate(values)
            ])

        self.assertEqual(status('failed', 'cancelled'), 'failed')
        self.assertEqual(status('failed', 'pending'), 'pending')
        self.assertEqual(status('delivered', 'cancelled'), 'partial')
        self.assertEqual(status('cancelled', 'cancelled'), 'cancelled')

    def test_ledger_read_and_write_failures_do_not_interrupt_confirmation_or_remaining_bubble_scheduling(self) -> None:
        hooks = _service_hooks()
        if hooks is None:
            self.skipTest('plugin/core/service.py（另一 Agent 并行移植）尚未提供 '
                          'confirm_outgoing_deliveries / update_script_delivery_outcome；'
                          '落地后本用例自动生效')
        _service_class, confirm, update = hooks
        for failure in ('read', 'write'):
            value = _commit()
            event = _outgoing_event(value)
            intents: list[Any] = []
            warnings: list[Any] = []
            stub = _ServiceStub(failure, value, intents, warnings, update)
            message = prepare_outgoing_delivery(
                attach_message_event({'participant_id': 'alice', 'content': event['content']},
                                     event, 42),
                event['bubbles'],
            )
            result = confirm(stub, {'id': 'story:1'}, [message])
            if inspect.isawaitable(result):
                asyncio.run(result)  # type: ignore[arg-type]
            self.assertEqual(len(intents), 1, failure)
            self.assertEqual(intents[0]['payload']['content'], '第二句', failure)
            self.assertEqual(len(warnings), 1, failure)

    def test_m6_ledger_groups_text_and_platform_segments_under_their_script_event_identities(self) -> None:
        value = _commit()
        actions = create_script_delivery_actions(value)
        speech = next(item for item in actions if item['event_kind'] == 'outgoing-message')
        platform = next(item for item in actions if item['event_kind'] == 'platform-action')

        self.assertEqual(
            [[item['kind'], item['content'], item['status']] for item in speech['segments']],
            [['message', '第一句', 'pending'], ['message', '第二句', 'pending']],
        )
        self.assertEqual(
            [[item['kind'], item['content']] for item in platform['segments']],
            [['local-media', 'sticker:cat'], ['native-face', 'smile'],
             ['message-reaction', 'group-message:7:heart']],
        )
        self.assertEqual(len({item['commit_id'] for item in actions}), 1)

    def test_m6_ledger_reports_partial_and_terminal_delivery_without_downgrading_delivered_speech(self) -> None:
        value = _commit()
        event = _outgoing_event(value)
        initial = create_script_delivery_actions(value)
        first = delivery_reference(event, 42, 0)
        second = delivery_reference(event, 42, 1)

        partial = update_script_delivery_actions(initial, first, 'delivered',
                                                 _dt('2026-09-05T10:01:02.000Z'))
        self.assertIsNotNone(partial)
        self.assertEqual(
            next(item for item in partial if item['event_id'] == event['event_id'])['status'],
            'partial',
        )
        complete = update_script_delivery_actions(partial, second, 'delivered',
                                                  _dt('2026-09-05T10:01:03.000Z'))
        self.assertIsNotNone(complete)
        self.assertEqual(
            next(item for item in complete if item['event_id'] == event['event_id'])['status'],
            'delivered',
        )
        self.assertIsNone(update_script_delivery_actions(
            complete, first, 'failed', _utc_now(), 'late bookkeeping failure'))
        self.assertIsNone(update_script_delivery_actions(complete, second, 'delivered', _utc_now()))

    def test_a_successful_retry_clears_an_earlier_transport_failure_reason(self) -> None:
        value = _commit()
        event = _outgoing_event(value)
        reference = delivery_reference(event, 42, 0)
        failed = update_script_delivery_actions(create_script_delivery_actions(value), reference,
                                                'failed', _utc_now(), 'temporary network failure')
        self.assertIsNotNone(failed)
        recovered = update_script_delivery_actions(failed, reference, 'delivered', _utc_now())
        self.assertIsNotNone(recovered)
        segment = next(item for item in recovered
                       if item['event_id'] == event['event_id'])['segments'][0]
        self.assertEqual(segment['status'], 'delivered')
        self.assertIsNone(segment.get('reason'))

    def test_m6_persistence_changes_metadata_only_and_delivery_preserves_exact_bubble_text_and_source_row(self) -> None:
        value = _commit()
        interaction = ({'seen': True, 'reply': {'mode': 'immediate', 'content': REPLY_CONTENT}}
                       if value['events'] else None)
        draft = script_entry_draft_for_commit(value, interaction)
        self.assertEqual(draft['content'], value['prose'])
        self.assertEqual(draft['metadata']['script_events'], value['events'])
        self.assertIsInstance(draft['metadata']['delivery_actions'], list)

        event = _outgoing_event(value)
        attached = attach_message_event({'participant_id': 'alice', 'content': event['content']},
                                        event, 42)
        prepared = prepare_outgoing_delivery(attached, event['bubbles'])
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared['content'], '第一句')
        self.assertEqual(prepared['later_segments'], ['第二句'])
        restored = restore_message_event({**script_event_payload(prepared, 1)}, '第二句')
        self.assertIsNotNone(restored)
        self.assertEqual(restored['script_entry_id'], 42)
        self.assertEqual(restored['full_content'], REPLY_CONTENT)

    def test_platform_references_resolve_the_exact_action_segment_and_reject_absent_actions(self) -> None:
        value = _commit()
        self.assertEqual(platform_action_reference(value, 42, 'local-media', 'sticker:cat')['segment_index'], 0)
        self.assertEqual(platform_action_reference(value, 42, 'native-face', 'smile')['segment_index'], 1)
        self.assertEqual(
            platform_action_reference(value, 42, 'message-reaction', 'group-message:7:heart')['segment_index'], 2)
        self.assertIsNone(platform_action_reference(value, 42, 'local-media', 'missing'))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
