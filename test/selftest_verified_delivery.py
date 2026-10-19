#!/usr/bin/env python3
"""Verified delivery and synchronous gateway retain against a synthetic loopback Hindsight. No real services."""
import asyncio
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

import selftest_interoperability as shared
import selftest_memory as fixture
from skirnir_router import memory, memory_gateway, memory_outbox, state
from skirnir_router.outcome import conflict
from skirnir_router.reliability_store import OPERATION_NOT_FOUND

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeOutbox:
    """In-memory outbox with the claim/finish semantics of ReliabilityStore (sends and verifications)."""
    def __init__(self):
        self.jobs = {}

    async def outbox_put(self, operation, scope, payload, fingerprint, capacity, owner_capacity=0):
        ident = str(operation)
        if ident in self.jobs:
            return self.jobs[ident] if self.jobs[ident]["fingerprint"] == fingerprint else conflict("event_id already used")
        self.jobs[ident] = {"operation_id": ident, "owner": scope["principal"], "project": scope["project"], "scope": scope,
                            "payload": payload, "fingerprint": fingerprint, "state": "pending", "attempts": 0, "retry_base": 0,
                            "upstream_attempts": 0, "upstream_base": 0, "verify_checks": 0, "accepted_at": None, "lease": None,
                            "last_error": None, "retry_rounds": 0, "delete_requested": False, "due": True,
                            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()}
        return self.jobs[ident]

    async def outbox_get(self, owner, operation):
        job = self.jobs.get(str(operation))
        return copy.deepcopy(job) if job and job["owner"] == owner else OPERATION_NOT_FOUND

    async def outbox_claim(self, lease_s, max_attempts):
        for job in self.jobs.values():
            if job["due"] and job["lease"] is None and job["state"] in ("pending", "delete_pending", "accepted"):
                if job["state"] == "accepted":
                    job["verify_checks"] += 1
                else:
                    job["state"], job["attempts"] = "processing", job["attempts"] + 1
                job["lease"] = str(uuid4())
                return copy.deepcopy(job)
        return None

    async def outbox_finish(self, operation, lease, status, delay=0, error=None, upstream_attempts=None):
        job = self.jobs[str(operation)]
        if job["lease"] != str(lease) or job["state"] not in ("processing", "accepted"):
            return conflict("outbox lease lost")
        if status == "accepted" and job["state"] == "processing":
            job["accepted_at"], job["verify_checks"] = datetime.now(timezone.utc).isoformat(), 0
        if upstream_attempts is not None:
            job["upstream_attempts"], job["retry_base"] = upstream_attempts, job["attempts"]
        job.update(state=status, lease=None, last_error=error, due=delay == 0, delay=delay)
        return None

    async def outbox_delete(self, owner, operation, scope):
        job = self.jobs[str(operation)]
        job.update(state="delete_pending", delete_requested=True, scope=scope, due=True)
        return None

    def release(self, ident):
        """Make a delayed job due now (the clock of the real store is available_at)."""
        self.jobs[ident]["due"] = True


class Harness(fixture.MemoryHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        shared.SharedGatewayTests.shared(self)
        self.service.worker.cancel()
        await asyncio.gather(self.service.worker, return_exceptions=True)
        self.service.worker = None
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, outbox_enabled=True, upstream_attempts=2, sync_wait_s=2)
        self.store = self.service.outbox = FakeOutbox()

    async def retain(self, event="synthetic-event"):
        response = await self.client.post(memory.PREFIX + "/retain", headers={"Authorization": "Bearer client-a"},
                                          json={"project": shared.SHARED, "event_id": event, "content": "Synthetic fact", "confirmed": True})
        self.assertEqual(response.status, 202)
        return (await response.json())["operation_id"]


class VerifiedDeliveryTests(Harness):
    async def test_send_then_verify_completes(self):
        operation = await self.retain()
        self.assertTrue(await memory_outbox.step(self.service))
        job = self.store.jobs[operation]
        self.assertEqual((job["state"], job["delay"]), ("accepted", self.service.cfg.verify_delay_s))
        self.store.release(operation)
        self.operation_state = "completed"
        await memory_outbox.step(self.service)
        self.assertEqual(self.store.jobs[operation]["state"], "completed")
        self.assertEqual(len(self.operations), 1)

    async def test_failed_extraction_is_resubmitted_under_new_id_same_document(self):
        operation = await self.retain()
        await memory_outbox.step(self.service)
        self.store.release(operation)
        self.operation_state = "failed"
        await memory_outbox.step(self.service)
        job = self.store.jobs[operation]
        self.assertEqual((job["state"], job["last_error"], job["upstream_attempts"]), ("pending", "upstream_failed", 1))
        self.assertEqual(job["delay"], self.service.cfg.upstream_backoff_s)
        self.store.release(operation)
        await memory_outbox.step(self.service)
        resubmitted = str(uuid5(UUID(operation), "resubmit-1"))
        self.assertIn(resubmitted, self.operations)
        self.assertEqual(self.operations[resubmitted]["items"][0]["document_id"], operation)
        self.assertEqual(self.store.jobs[operation]["state"], "accepted")
        self.store.release(operation)
        self.operation_state = "completed"
        await memory_outbox.step(self.service)
        self.assertEqual(self.store.jobs[operation]["state"], "completed")

    async def test_resubmissions_are_bounded(self):
        operation = await self.retain()
        self.operation_state = "failed"
        for _ in range(3):   # upstream_attempts=2: two resubmissions, then dead
            self.store.release(operation)
            await memory_outbox.step(self.service)   # send
            self.store.release(operation)
            await memory_outbox.step(self.service)   # verify -> failed
        job = self.store.jobs[operation]
        self.assertEqual((job["state"], job["last_error"]), ("dead", "upstream_failed"))
        self.assertEqual(len(self.operations), 3)

    async def test_unknown_and_stuck_operations_are_dead_letters(self):
        operation = await self.retain()
        await memory_outbox.step(self.service)
        self.store.release(operation)
        self.operation_state = "not_found"
        self.service.client.operation = lambda *args, **kwargs: _value({"status": "not_found"})
        await memory_outbox.step(self.service)
        self.assertEqual((self.store.jobs[operation]["state"], self.store.jobs[operation]["last_error"]), ("dead", "remote_unknown"))
        second = await self.retain("second")
        await memory_outbox.step(self.service)
        self.store.jobs[second]["accepted_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.store.release(second)
        self.service.client.operation = lambda *args, **kwargs: _value({"status": "processing"})
        await memory_outbox.step(self.service)
        self.assertEqual((self.store.jobs[second]["state"], self.store.jobs[second]["last_error"]), ("dead", "remote_pending"))

    async def test_pending_extraction_is_checked_again_with_growing_delay(self):
        operation = await self.retain()
        await memory_outbox.step(self.service)
        self.operation_state = "processing"
        delays = []
        for _ in range(3):
            self.store.release(operation)
            await memory_outbox.step(self.service)
            delays.append(self.store.jobs[operation]["delay"])
        self.assertEqual(self.store.jobs[operation]["state"], "accepted")
        self.assertEqual(delays, sorted(delays))
        self.assertGreater(delays[-1], delays[0])

    async def test_delete_after_upstream_failure_does_not_wait_forever(self):
        operation = await self.retain()
        await memory_outbox.step(self.service)
        await self.store.outbox_delete("client-a", UUID(operation), {})
        self.store.jobs[operation]["scope"] = {**self.store.jobs[operation]["scope"], "__bank": self.service.bank(memory.scope_for_principal("client-a", shared.SHARED))}
        self.store.jobs[operation]["scope"].update(principal="client-a", project=shared.SHARED, data_class="personal", read=True,
                                                   write=True, allow_cloud=False, shared_scope="example-team", delete=True, strategy="legacy")
        self.operation_state = "failed"
        await memory_outbox.step(self.service)
        self.assertEqual(self.store.jobs[operation]["state"], "deleted")
        self.assertEqual(self.deleted_documents[-1][1], operation)


class SynchronousGatewayTests(Harness):
    async def gateway(self, payload, token="client-a"):
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + shared.SHARED + "/memories"
        return await self.client.post(path, json=payload, headers={"Authorization": "Bearer " + token})

    async def test_sync_retain_answers_after_verified_completion(self):
        self.operation_state = "completed"
        worker = asyncio.create_task(_drive(self.service, self.store))
        try:
            response = await self.gateway({"items": [{"content": "Synthetic synchronous fact"}]})
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(response.status, 200, await response.text())
        value = await response.json()
        self.assertFalse(value["async"])
        self.assertEqual(self.store.jobs[value["operation_id"]]["state"], "completed")
        self.assertEqual(self.service.sync_waiting, 0)

    async def test_sync_retain_times_out_honestly_and_job_stays_durable(self):
        self.operation_state = "processing"
        worker = asyncio.create_task(_drive(self.service, self.store))
        try:
            response = await self.gateway({"async": False, "items": [{"content": "Synthetic slow fact"}]})
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(response.status, 504)
        text = await response.text()
        self.assertIn("operation_id=", text)
        self.assertIn("not yet confirmed", text)
        self.assertEqual([job["state"] for job in self.store.jobs.values()], ["accepted"])
        self.assertEqual(self.service.sync_waiting, 0)

    async def test_sync_waiting_slots_are_bounded(self):
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, sync_max_waiting=1)
        self.service.sync_waiting = 1
        response = await self.gateway({"items": [{"content": "Synthetic fact"}]})
        self.assertEqual(response.status, 503)
        self.assertEqual(self.store.jobs, {})
        self.service.sync_waiting = 0

    async def test_async_retain_unchanged_and_invalid_flag_rejected(self):
        response = await self.gateway({"async": True, "items": [{"content": "Synthetic fact"}]})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["async"])
        self.assertEqual((await self.gateway({"async": "yes", "items": [{"content": "Synthetic fact"}]})).status, 400)

    async def test_status_of_resubmitted_job_is_in_progress_not_failed(self):
        response = await self.gateway({"async": True, "items": [{"content": "Synthetic fact"}]})
        operation = (await response.json())["operation_id"]
        await memory_outbox.step(self.service)
        self.operation_state = "failed"
        status = await self.client.get(memory_gateway.GATEWAY + "/v1/default/banks/" + shared.SHARED + "/operations/" + operation,
                                       headers={"Authorization": "Bearer client-a"})
        self.assertEqual((await status.json())["status"], "processing")


async def _value(value):
    return value


async def _drive(service, store):
    """Worker stand-in: process due jobs and make delayed verifications due quickly."""
    while True:
        while await memory_outbox.step(service) is True:
            pass
        await asyncio.sleep(0.1)
        for ident, job in store.jobs.items():
            if job["state"] == "accepted":
                store.release(ident)


if __name__ == "__main__":
    unittest.main(verbosity=2)
