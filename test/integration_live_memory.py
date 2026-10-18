#!/usr/bin/env python3
"""Opt-in real extraction and official Hermes-provider smoke tests.

Requires disposable PostgreSQL and a loopback Hindsight service with a configured
model. No production endpoint, conversation or model credentials are embedded.
No service is started and no model is contacted without explicit opt-in.
"""
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID, uuid4

import selftest_interoperability as shared
import selftest_memory as fixture
from skirnir_router import hindsight, memory, memory_gateway, memory_outbox, state
from skirnir_router.session_config import SessionConfig
from skirnir_router.session_store import PostgresSessionStore

ENABLED = os.environ.get('SKIRNIR_RUN_LIVE_MEMORY_TEST') == '1'


@unittest.skipUnless(ENABLED, 'real service not tested: set SKIRNIR_RUN_LIVE_MEMORY_TEST=1 explicitly')
class LiveMemoryTests(fixture.MemoryHarness):
    async def asyncSetUp(self):
        # Fail when explicitly enabled but incompletely configured; never report a
        # successful smoke test by silently skipping a missing backend.
        dsn = os.environ.get('SKIRNIR_TEST_SESSION_DSN')
        if dsn is None and os.environ.get('SKIRNIR_LIVE_POSTGRES_ENV') == '1' and os.environ.get('PGHOST') in ('localhost', '127.0.0.1', '::1'):
            dsn = ''  # asyncpg uses explicit disposable PG* environment settings
        if dsn is None or not os.environ.get('SKIRNIR_TEST_HINDSIGHT_URL'):
            self.fail('live test requires a disposable database and loopback Hindsight URL')
        await super().asyncSetUp()
        shared.SharedGatewayTests.shared(self)
        self.service.worker.cancel()
        await asyncio.gather(self.service.worker, return_exceptions=True)
        self.service.worker = None
        await self.service.client.close()
        cfg = replace(self.service.cfg, outbox_enabled=True, endpoint_env='SKIRNIR_TEST_HINDSIGHT_URL',
                      allow_http_loopback=True, timeout_s=30, recall_max_tokens=4096)
        endpoint = hindsight.endpoint_from_env(cfg)
        from urllib.parse import urlsplit
        import ipaddress
        host = urlsplit(endpoint).hostname
        if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
            self.fail('live tests accept loopback backends only')
        self.service.cfg = state.CFG.hindsight = cfg
        self.service.key = uuid4().hex.encode()
        self.service.client = hindsight.HindsightClient(endpoint, os.environ.get('SKIRNIR_TEST_HINDSIGHT_TOKEN', ''), cfg)
        self.real = await PostgresSessionStore.open(dsn, SessionConfig(enabled=True))
        self.service.outbox = self.real
        from hindsight_client import Hindsight
        self.sdk = Hindsight(base_url=str(self.client.make_url(memory_gateway.GATEWAY)), api_key='client-a', timeout=30)
        self.operations_to_delete = {}
        self.provider = None

    async def asyncTearDown(self):
        if self.provider is not None:
            await asyncio.to_thread(self.provider.shutdown)
        # Attempt cleanup even when recall fails; only this run's immutable source
        # IDs are targeted. Never delete a whole shared bank.
        failures = 0
        for operation, owner in self.operations_to_delete.items():
            try:
                await self.wait_completed(operation, owner)
                await self.service.client.delete_document(self.service.bank(memory.scope_for_principal(owner, shared.SHARED)), operation)
            except (AssertionError, hindsight.MemoryUnavailable, TimeoutError):
                failures += 1
        await self.sdk.aclose()
        async with self.real.pool.acquire() as conn:
            await conn.execute("DELETE FROM skirnir_sessions.memory_outbox WHERE owner=ANY($1::text[]) AND scope->>'__bank'=$2", ['client-a', 'client-b'],
                               self.service.bank(memory.scope_for_principal('client-a', shared.SHARED)))
        await self.real.close()
        await super().asyncTearDown()
        self.assertEqual(failures, 0, 'live source cleanup incomplete; inspect isolated test service')

    async def wait_completed(self, operation, owner="client-a"):
        async with asyncio.timeout(180):
            while True:
                response = await self.client.get(memory.PREFIX + '/operations/' + operation, headers={'Authorization': 'Bearer ' + owner})
                self.assertEqual(response.status, 200)
                value = await response.json()
                self.assertNotIn(value.get('upstream_status'), ('failed', 'cancelled'))
                if value.get('upstream_status') == 'completed':
                    return
                await asyncio.sleep(1)

    async def submit(self, content):
        response = await self.sdk.aretain_batch(bank_id=shared.SHARED, items=[{'content': content}], retain_async=True)
        operation = str(response.operation_id)
        self.operations_to_delete[operation] = 'client-a'
        await memory_outbox.step(self.service)
        await self.wait_completed(operation)
        return operation

    async def test_real_extraction_cross_client_recall_and_selective_deletion(self):
        marker = 'synthetic-' + uuid4().hex[:12]
        content = f'The fictional research project {marker} uses a violet paper boat as its test symbol.'
        operation = await self.submit(content)
        async with asyncio.timeout(180):
            while True:
                response = await self.client.post(memory.PREFIX + '/recall', json={'project': shared.SHARED, 'query': marker},
                                                  headers={'Authorization': 'Bearer client-b'})
                self.assertEqual(response.status, 200)
                value = await response.json()
                if marker in json.dumps(value['items']).lower():
                    break
                await asyncio.sleep(1)
        response = await self.client.post(memory.PREFIX + '/recall', json={'project': shared.SHARED, 'query': marker},
                                          headers={'Authorization': 'Bearer client-owned'})
        self.assertEqual(response.status, 403)
        response = await self.client.post(memory.PREFIX + '/operations/' + operation + '/delete', json={}, headers={'Authorization': 'Bearer client-a'})
        self.assertEqual(response.status, 202)
        await memory_outbox.step(self.service)
        row = await self.real.outbox_get('client-a', UUID(operation))
        self.assertEqual(row['state'], 'deleted')
        self.operations_to_delete.pop(operation)
        bank = self.service.bank(memory.scope_for_principal('client-a', shared.SHARED))
        document = await self.service.client.control(bank, '/documents/' + operation)
        self.assertEqual(document.get('status'), 'not_found')

    async def test_actual_installed_hermes_provider_tools(self):
        directory = os.environ.get('SKIRNIR_TEST_HERMES_PLUGIN')
        if not directory:
            self.skipTest('official Hermes provider not tested: set SKIRNIR_TEST_HERMES_PLUGIN to installed plugin directory')
        # Run in the installed Hermes environment, using its real MemoryProvider
        # dependencies. Configuration alone is replaced; no HTTP/SDK is mocked.
        path = Path(directory).resolve()
        spec = importlib.util.spec_from_file_location('skirnir_live_hindsight_plugin', path / '__init__.py', submodule_search_locations=[str(path)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cfg = {'mode': 'local_external', 'api_url': str(self.client.make_url(memory_gateway.GATEWAY)), 'apiKey': 'client-b',
               'bank_id': shared.SHARED, 'auto_retain': False, 'auto_recall': True, 'memory_mode': 'hybrid',
               'retain_async': True, 'recall_types': ['world', 'experience', 'observation']}
        self.provider = module.HindsightMemoryProvider()
        # Avoid ambient profile secrets/project overrides; use only synthetic test
        # identity and the ephemeral router address above.
        with patch.object(module, '_load_config', return_value=cfg), patch.object(module, 'get_secret', side_effect=lambda name, default='': default):
            await asyncio.to_thread(self.provider.initialize, 'synthetic-live-session')
        marker = 'synthetic-' + uuid4().hex[:12]
        content = f'The fictional project {marker} has an amber wooden kite as its test symbol.'
        # Write through the actual Hermes tool, then replay identical content via
        # the official SDK to obtain the same gateway-deduplicated operation UUID.
        result = await asyncio.to_thread(self.provider.handle_tool_call, 'hindsight_retain', {'content': content})
        self.assertNotIn('error', json.loads(result))
        # The Hermes writer owns its operation; client-b polls/cleans it through
        # the SDK-compatible gateway, avoiding cross-owner status access.
        from hindsight_client import Hindsight
        peer = Hindsight(base_url=str(self.client.make_url(memory_gateway.GATEWAY)), api_key='client-b', timeout=30)
        try:
            retained = await peer.aretain_batch(bank_id=shared.SHARED, items=[{'content': content, 'observation_scopes': 'shared'}], retain_async=True)
            operation = str(retained.operation_id)
            self.operations_to_delete[operation] = 'client-b'
            await memory_outbox.step(self.service)
            async with asyncio.timeout(180):
                while True:
                    status = await peer.operations.get_operation_status(bank_id=shared.SHARED, operation_id=operation)
                    self.assertNotIn(status.status, ('failed', 'cancelled'))
                    if status.status == 'completed':
                        break
                    await asyncio.sleep(1)
            result = await asyncio.to_thread(self.provider.handle_tool_call, 'hindsight_recall', {'query': marker})
            self.assertIn(marker, result.lower())
            response = await self.client.post(memory.PREFIX + '/recall', json={'project': shared.SHARED, 'query': marker}, headers={'Authorization': 'Bearer client-a'})
            self.assertIn(marker, json.dumps(await response.json()).lower())
        finally:
            await peer.aclose()


if __name__ == '__main__':
    # Do not print live endpoints, model errors, credentials or plugin debug logs.
    logging.disable(logging.CRITICAL)
    try:
        result = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, 'w')).run(unittest.defaultTestLoader.loadTestsFromTestCase(LiveMemoryTests))
        print(json.dumps({'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                          'skipped': len(result.skipped), 'real_service_requested': ENABLED}))
        sys.exit(not result.wasSuccessful())
    except Exception:  # noqa: BLE001 -- redact external model/plugin exceptions
        print('Live memory test failed; inspect the isolated test environment without publishing secrets.')
        sys.exit(1)
