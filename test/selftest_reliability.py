#!/usr/bin/env python3
"""Synthetic quality, diagnostics, retry authorization and worker contracts."""
import asyncio
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

import selftest_interoperability as shared
import selftest_memory as fixture
from skirnir_router import memory, memory_gateway, memory_outbox, state
from skirnir_router.context_config import ContextConfig, parse_context_config
from skirnir_router.context_manager import project
from skirnir_router.outcome import failed, unavailable
from skirnir_router.memory_config import parse_hindsight
from skirnir_router.provenance import recall_metadata


class QualityTests(unittest.TestCase):
    def test_source_references_are_whole_bounded_values(self):
        refs = ['repo:' + 'x' * 90, 'artifact:' + 'y' * 190]
        value = recall_metadata({'source_refs': json.dumps(refs), 'kind': 'user_assertion'})
        self.assertEqual(json.loads(value['source_refs']), refs)
        self.assertNotIn('source_refs', recall_metadata({'source_refs': '[broken'}))
        self.assertNotIn('source_refs', recall_metadata({'source_refs': ['x' * 257]}))
        self.assertNotIn('kind', recall_metadata({'kind': {'untrusted': 'object'}}))

    def test_optional_limits_are_strict(self):
        self.assertEqual(parse_hindsight(None).outbox_workers, 1)
        self.assertEqual(parse_hindsight(None).outbox_client_capacity, 0)
        for raw in ({'outbox_workers': 0}, {'outbox_workers': True}, {'outbox_client_capacity': -1}):
            with self.subTest(raw=raw):
                self.assertTrue(failed(parse_hindsight(raw)))
        self.assertTrue(failed(parse_context_config({'tool_result_strategy': 'unknown'})))

    def test_head_tail_keeps_decision_error_checkpoint_and_original(self):
        context = shared.history()
        context['messages'][2] = shared.row(3, 'assistant', '', tool_calls=[{'id': 'example', 'function': {'name': 'example', 'arguments': {}}}])
        context['messages'][3] = shared.row(4, 'tool', 'DECISION_USE_TRANSACTION\n' + 'Synthetic middle. ' * 3000 + '\nFINAL_ERROR_RETRY_REQUIRED', tool_call_id='example')
        context['checkpoint'] = {'version': 1, 'payload': {'schema_version': 1, 'open_steps': ['VERIFY_MIGRATION_BEFORE_CONTINUING']}}
        for entry in context['messages']:
            if entry['seq'] in (18, 19):
                entry['payload']['content'] = 'Synthetic recent conversation'
        original = copy.deepcopy(context)
        cfg = ContextConfig(tail_messages=2, reduce_tool_results=True, tool_result_bytes=256, tool_result_strategy='head_tail')
        messages, version = project(context, {}, 7000, cfg, 'synthetic-small-model')
        text = json.dumps(messages)
        for marker in ('DECISION_USE_TRANSACTION', 'FINAL_ERROR_RETRY_REQUIRED', 'VERIFY_MIGRATION_BEFORE_CONTINUING', 'Current question'):
            self.assertIn(marker, text)
        self.assertTrue(any(m.get('tool_call_id') == 'example' for m in messages))
        self.assertEqual(context, original)
        self.assertIn(4, version['payload']['provenance']['reduced_tool_sequences'])
        projected = {**context, 'compaction': {**version, 'version': 1},
                     'messages': [r for r in context['messages'] if r['seq'] > version['through_seq']]}
        for _ in range(4):
            base = max(r['seq'] for r in projected['messages'])
            projected['messages'].extend([shared.row(base + 1, 'user', 'Synthetic intermediate work. ' * 1000),
                                          shared.row(base + 2, 'assistant', 'Synthetic answer'),
                                          shared.row(base + 3, 'user', 'Current question'),
                                          shared.row(base + 4, 'assistant', 'Synthetic response')])
            messages, next_version = project(projected, {}, 5000, cfg, 'synthetic-smaller-model')
            self.assertIn('FINAL_ERROR_RETRY_REQUIRED', json.dumps(messages))
            if next_version:
                projected = {**projected, 'compaction': {**next_version, 'version': 2},
                             'messages': [r for r in projected['messages'] if r['seq'] > next_version['through_seq']]}



class ReliabilityApiTests(fixture.MemoryHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        shared.SharedGatewayTests.shared(self)
        self.store = await shared.SharedGatewayTests.durable(self)

    async def api(self, path, data, token='client-a'):
        return await self.client.post(path, json=data, headers={'Authorization': 'Bearer ' + token})

    async def retain(self, event='synthetic-event'):
        data = {'project': shared.SHARED, 'event_id': event, 'content': 'Synthetic fact', 'confirmed': True}
        response = await self.api(memory.PREFIX + '/retain', data)
        self.assertEqual(response.status, 202)
        return (await response.json())['operation_id']

    async def test_retry_rechecks_owner_write_permission_and_preserves_identity(self):
        operation = await self.retain()
        path = memory.PREFIX + '/operations/' + operation + '/retry'
        self.store.outbox_retry.return_value = {'state': 'pending'}
        response = await self.api(path, {'retry_id': 'recovery-1'})
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())['operation_id'], operation)
        self.assertEqual(str(self.store.outbox_retry.call_args.args[1]), operation)
        self.assertEqual((await self.api(path, {'retry_id': 'recovery-1'}, 'client-b')).status, 404)
        state.CFG.client_auth['clients']['client-a']['memory']['projects'][shared.SHARED]['write'] = False
        self.store.outbox_retry.reset_mock()
        self.assertEqual((await self.api(path, {'retry_id': 'recovery-2'})).status, 403)
        self.store.outbox_retry.assert_not_awaited()

    async def test_error_category_never_contains_upstream_body(self):
        await self.retain()
        self.service.client.retain = AsyncMock(return_value=unavailable('SYNTHETIC_BODY_MUST_NOT_LEAK', 'upstream_auth'))
        await memory_outbox.step(self.service)
        self.assertEqual(self.store.outbox_finish.call_args.kwargs, {'error': 'upstream_auth'})
        self.assertNotIn('SYNTHETIC_BODY', str(self.store.outbox_finish.call_args))

    async def test_hermes_default_shared_observation_scope_is_forwarded(self):
        path = memory_gateway.GATEWAY + '/v1/default/banks/' + shared.SHARED + '/memories'
        item = {'content': 'Synthetic provider knowledge', 'observation_scopes': 'shared'}
        response = await self.api(path, {'async': True, 'items': [item]})
        self.assertEqual(response.status, 200)
        operation = (await response.json())['operation_id']
        await memory_outbox.step(self.service)
        self.assertEqual(self.operations[operation]['items'][0]['observation_scopes'], 'shared')
        response = await self.api(path, {'async': True, 'items': [{**item, 'observation_scopes': 'unconfigured'}]})
        self.assertEqual(response.status, 400)

    async def test_bounded_parallel_workers(self):
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, outbox_workers=2)
        for event in ('one', 'two', 'three'):
            await self.retain(event)
        release, entered = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0
        async def send(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
            finally:
                active -= 1
        self.service.client.retain = AsyncMock(side_effect=send)
        worker = asyncio.create_task(memory_outbox.run(self.service))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(peak, 2)
            self.assertEqual(sum(v['state'] == 'pending' for v in self.jobs.values()), 1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(active, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
