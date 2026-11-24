#!/usr/bin/env python3
"""Memory backend interface (ADR-0115): the contract every adapter passes, the router's policy on typed facts, and the
Hindsight-compatible gateway byte for byte. Synthetic loopback fakes only; no real service or user data."""
import asyncio
import copy
import hashlib
import json
import unittest
from dataclasses import replace

import selftest_interoperability as shared
import selftest_memory as fixture
import selftest_verified_delivery as delivery
from aiohttp import web
from skirnir_router import config, memory, memory_backend, memory_gateway, memory_outbox, state
from skirnir_router.memory_backend import KINDS, NOT_FOUND, Fact, FactList, MemoryPage, OpState
from skirnir_router.memory_config import HindsightConfig, parse_hindsight
from skirnir_router.outcome import failed, unavailable

PROJECT = fixture.PROJECT


class BackendContract:
    """What every memory backend adapter must do, whatever system it talks to. A test class mixes this in next to an
    IsolatedAsyncioTestCase and provides the hooks below with a fake of its system that answers in that system's own
    wire format: backend, bank(), serve_facts(), serve_missing_bank(), serve_failure(), serve_operation(), serve_units(),
    set_healthy() and requests() (how many requests reached the fake). Facts are given in neutral terms: dicts with
    id, text and optionally type, score, sources, metadata, created_at, updated_at, document_id, date."""
    FAILURES = ("down", "malformed", "oversized")

    async def test_health_is_reported_as_a_fact(self):
        self.set_healthy(True)
        self.assertIs(await self.backend.health(), True)
        self.set_healthy(False)
        self.assertIs(await self.backend.health(), False)

    async def test_recall_returns_typed_facts_in_the_backend_ranking(self):
        self.serve_facts([
            {"id": "fact-1", "text": "Synthetic first statement.", "type": "world", "score": 0.9,
             "metadata": {"kind": "confirmed_information"}, "created_at": "2026-01-02T00:00:00Z"},
            {"id": "obs-1", "text": "Synthetic observation.", "type": "observation", "sources": ["fact-1"], "score": 0.5},
            {"id": "fact-2", "text": "Synthetic second statement.", "updated_at": "2026-01-03T00:00:00Z"}])
        result = await self.backend.recall(self.bank(), "Synthetic question")
        self.assertIsInstance(result, FactList)
        self.assertFalse(result.bank_missing)
        self.assertTrue(all(isinstance(fact, Fact) for fact in result.facts))
        self.assertEqual([fact.id for fact in result.facts], ["fact-1", "obs-1", "fact-2"])
        first, observation, second = result.facts
        self.assertEqual((first.text, first.type, first.score, first.metadata, first.created_at),
                         ("Synthetic first statement.", "world", 0.9, {"kind": "confirmed_information"}, "2026-01-02T00:00:00Z"))
        self.assertEqual((observation.type, observation.sources), ("observation", ("fact-1",)))
        self.assertEqual((second.score, second.sources, second.metadata, second.updated_at), (None, (), None, "2026-01-03T00:00:00Z"))

    async def test_a_missing_bank_is_a_state_not_an_empty_answer(self):
        self.serve_missing_bank()
        self.assertEqual(await self.backend.recall(self.bank(), "Synthetic question"), FactList((), bank_missing=True))
        self.assertEqual(await self.backend.list(self.bank()), MemoryPage((), 0))

    async def test_no_usable_answer_is_a_problem_with_a_fixed_kind(self):
        for failure in self.FAILURES:
            with self.subTest(failure=failure):
                self.serve_failure(failure)
                result = await self.backend.recall(self.bank(), "Synthetic question")
                self.assertTrue(failed(result))
                self.assertEqual(result.status, 503)
                self.assertIn(result.kind, KINDS)
        self.serve_failure(None)

    async def test_a_policy_change_before_sending_sends_nothing(self):
        bank, before = self.bank(), self.requests()
        no = lambda: False   # noqa: E731
        calls = (self.backend.recall(bank, "Synthetic", authorize=no),
                 self.backend.retain(bank, "Synthetic", "00000000-0000-5000-8000-000000000001", confirmed=True, authorize=no),
                 self.backend.operation(bank, "00000000-0000-5000-8000-000000000001", authorize=no),
                 self.backend.delete_document(bank, "00000000-0000-5000-8000-000000000001", authorize=no),
                 self.backend.list(bank, authorize=no))
        for result in await asyncio.gather(*calls):
            self.assertTrue(failed(result))
            self.assertEqual(result.status, 403)
        self.assertEqual(self.requests(), before)

    async def test_a_bank_id_of_another_form_is_refused_without_a_request(self):
        before = self.requests()
        for bank in ("other-bank", "skirnir-" + "0" * 63, "skirnir-" + "0" * 64 + "/x"):
            with self.subTest(bank=bank):
                self.assertTrue(failed(await self.backend.recall(bank, "Synthetic")))
                self.assertTrue(failed(await self.backend.retain(bank, "Synthetic", "00000000-0000-5000-8000-000000000002", confirmed=True)))
                self.assertTrue(failed(await self.backend.list(bank)))
        self.assertEqual(self.requests(), before)

    async def test_retain_is_acknowledged_and_idempotent_per_operation(self):
        operation = "00000000-0000-5000-8000-000000000003"
        self.assertIsNone(await self.backend.retain(self.bank(), "Synthetic retained fact", operation, confirmed=True))
        self.assertIsNone(await self.backend.retain(self.bank(), "Synthetic retained fact", operation, confirmed=True))

    async def test_operation_states_are_typed(self):
        operation = "00000000-0000-5000-8000-000000000004"
        for status in ("pending", "processing", "completed", "failed", "cancelled"):
            with self.subTest(status=status):
                self.serve_operation(status)
                self.assertEqual(await self.backend.operation(self.bank(), operation), OpState(status))
        self.serve_operation(NOT_FOUND)
        self.assertEqual(await self.backend.operation(self.bank(), operation), OpState(NOT_FOUND))

    async def test_delete_document_reports_done_also_when_already_gone(self):
        document = "00000000-0000-5000-8000-000000000005"
        self.assertIsNone(await self.backend.delete_document(self.bank(), document))
        self.serve_missing_bank()
        self.assertIsNone(await self.backend.delete_document(self.bank(), document))

    async def test_list_returns_a_typed_page(self):
        self.serve_units([{"id": "unit-1", "text": "Synthetic listed fact.", "type": "world", "document_id": "doc-1",
                           "sources": ["unit-0"], "date": "2026-01-01T00:00:00Z"},
                          {"id": "unit-2", "text": "Synthetic second unit.", "type": "observation"}])
        page = await self.backend.list(self.bank(), limit=10)
        self.assertIsInstance(page, MemoryPage)
        self.assertEqual(page.total, 2)
        self.assertEqual([(f.id, f.type, f.document_id, f.sources, f.date) for f in page.facts],
                         [("unit-1", "world", "doc-1", ("unit-0",), "2026-01-01T00:00:00Z"), ("unit-2", "observation", None, (), None)])


class HindsightContractTests(BackendContract, fixture.MemoryHarness):
    """The contract against the Hindsight adapter and the synthetic loopback Hindsight of selftest_memory."""
    async def asyncSetUp(self):
        self.operation_requests = 0
        await super().asyncSetUp()

    @property
    def backend(self):
        return self.service.backend

    def bank(self):
        return self.service.bank(memory.scope_for_principal("client-a", PROJECT, read=True))

    def requests(self):
        return len(self.calls) + self.operation_requests + len(self.deleted_documents) + len(self.list_queries)

    def set_healthy(self, healthy):
        self.health_reply = (200, {"status": "healthy"}) if healthy else (503, {"status": "unhealthy"})

    def serve_facts(self, facts):
        def raw(fact):
            value = {k: v for k, v in fact.items() if k not in ("score", "sources")}
            if "score" in fact:
                value["scores"] = {"reranker": fact["score"]}
            if "sources" in fact:
                value["source_fact_ids"] = fact["sources"]
            return value
        self.results = [raw(fact) for fact in facts]

    def serve_missing_bank(self):
        self.failure_status = 404

    def serve_failure(self, failure):
        self.failure_status = 500 if failure == "down" else 0
        self.bad_json, self.large_response = failure == "malformed", failure == "oversized"

    def serve_operation(self, status):
        self.operation_state = status

    def serve_units(self, units):
        renamed = {"type": "fact_type", "sources": "source_memory_ids"}
        self.list_units = [{renamed.get(k, k): v for k, v in unit.items()} for unit in units]

    async def fake_operation(self, request):
        self.operation_requests += 1
        if self.operation_state == NOT_FOUND:   # Hindsight answers an unknown operation with HTTP 404
            return web.json_response({"detail": "synthetic not found"}, status=404)
        return await super().fake_operation(request)

    async def fake_delete(self, request):
        if self.failure_status == 404:
            self.deleted_documents.append((request.match_info["bank"], request.match_info["document"]))
            return web.json_response({"detail": "synthetic not found"}, status=404)
        return await super().fake_delete(request)

    async def fake_list(self, request):
        if self.failure_status == 404:
            self.list_queries.append((request.match_info["bank"], dict(request.query)))
            return web.json_response({"detail": "synthetic not found"}, status=404)
        return await super().fake_list(request)

    async def test_hindsight_answers_are_read_defensively(self):
        # Hindsight-specific shapes: malformed entries are dropped one by one, a malformed answer is a protocol problem.
        self.results = ["not-an-object", {"id": 5, "text": "bad id"}, {"id": "no-text"},
                        {"id": "ok", "text": "Synthetic.", "scores": {"reranker": True}, "source_fact_ids": ["a", 5], "type": 7}]
        result = await self.backend.recall(self.bank(), "Synthetic")
        self.assertEqual(result.facts, (Fact("ok", "Synthetic."),))
        self.results = [{"id": f"fact-{i}", "text": "Synthetic."} for i in range(150)]
        self.assertEqual(len((await self.backend.recall(self.bank(), "Synthetic")).facts), 100)
        self.operation_state = 42
        self.assertEqual(await self.backend.operation(self.bank(), "00000000-0000-5000-8000-000000000006"), OpState("unknown"))
        self.list_units = [{"id": "u", "fact_type": 3, "source_memory_ids": ["s", 1] + ["x"] * 40}, "junk", {"text": "no id"}]
        page = await self.backend.list(self.bank())
        self.assertEqual([(f.id, f.text, f.type, len(f.sources)) for f in page.facts], [("u", "", None, 31)])

    async def test_hindsight_unreachable_is_a_network_problem(self):
        await self.hindsight.close()
        result = await self.backend.recall(self.bank(), "Synthetic")
        self.assertEqual(result.status, 503)
        self.assertIn(result.kind, ("transport", "timeout"))   # Windows retries a refused connect until the timeout
        self.assertIs(await self.backend.health(), False)


class FakeBackend:
    """A MemoryBackend that serves typed Facts: the router's policy runs on these alone, without any wire format."""
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    async def recall(self, bank, query, *, types=None, authorize=None):
        self.calls += 1
        if authorize is not None and not authorize():
            return memory_backend.POLICY_CHANGED
        return self.answer

    async def close(self):
        pass


class PolicyOnTypedFactsTests(fixture.MemoryHarness):
    """Budget, minimum score, duplicates, content filter, provenance and scope checks are the router's, for any backend."""
    async def use(self, answer, **settings):
        real, self.fake = self.service.backend, FakeBackend(answer)
        self.service.backend = self.fake
        await real.close()
        if settings:
            self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, **settings)

    async def recall(self, text_budget=None):
        return await self.service.recall(memory.scope_for_principal("client-a", PROJECT, read=True), "Synthetic question", text_budget=text_budget)

    async def test_minimum_score_needs_a_score_in_range(self):
        await self.use(FactList((Fact("a", "Synthetic a.", score=0.2), Fact("b", "Synthetic b.", score=0.8), Fact("c", "Synthetic c."),
                                 Fact("d", "Synthetic d.", score=1.5))), recall_min_score=0.5)
        self.assertEqual([item["source"] for item in (await self.recall()).items], ["b"])

    async def test_fact_count_and_client_budget_limit_the_answer(self):
        facts = FactList(tuple(Fact(f"fact-{i}", f"Synthetic statement number {i}.") for i in range(5)))
        await self.use(facts, recall_max_facts=3)
        recalled = await self.recall()
        self.assertEqual((len(recalled.items), recalled.limited_by), (3, "facts"))
        recalled = await self.recall(text_budget=40)
        self.assertEqual((len(recalled.items), recalled.limited_by, recalled.trimmed), (1, "client", 4))   # a shorter later fact could still fit, so all are tried

    async def test_a_single_source_observation_is_a_duplicate(self):
        await self.use(FactList((Fact("fact-1", "Synthetic statement."),
                                 Fact("obs-1", "Synthetic observation.", type="observation", sources=("fact-1",)),
                                 Fact("obs-2", "Synthetic summary.", type="observation", sources=("fact-1", "fact-2")))))
        recalled = await self.recall()
        self.assertEqual(([item["source"] for item in recalled.items], recalled.duplicates), (["fact-1", "obs-2"], 1))

    async def test_filtered_ids_texts_and_provenance_never_pass(self):
        await self.use(FactList((Fact("a", "Contact person@example.com"), Fact("bad\nid", "Synthetic."), Fact("x" * 129, "Synthetic."),
                                 Fact("b", "Synthetic.", metadata={"origin_client": "person@example.com"}),
                                 Fact("c", "Synthetic kept.", metadata={"kind": "assistant_derivation", "unknown_key": "dropped"},
                                      created_at="2026-01-01T00:00:00Z" + "x" * 100))))
        recalled = await self.recall()
        self.assertEqual(recalled.items, [{"source": "c", "text": "Synthetic kept.", "provenance": {"kind": "assistant_derivation"},
                                           "created_at": ("2026-01-01T00:00:00Z" + "x" * 100)[:64]}])

    async def test_backend_states_become_recall_states(self):
        await self.use(FactList((), bank_missing=True))
        self.assertEqual((await self.recall()).state, "bank_missing")
        await self.use(unavailable("memory backend unavailable", "timeout"))
        self.assertEqual((await self.recall()).state, "unavailable:timeout")
        await self.use(memory_backend.POLICY_CHANGED)
        self.assertEqual((await self.recall()).status, 403)

    async def test_a_revoked_grant_reaches_no_backend(self):
        await self.use(FactList((Fact("a", "Synthetic."),)))
        scope = memory.scope_for_principal("client-a", PROJECT, read=True)
        state.CFG.client_auth["clients"]["client-a"]["memory"]["projects"][PROJECT]["read"] = False
        self.assertEqual((await self.service.recall(scope, "Synthetic")).status, 403)
        self.assertEqual(self.fake.calls, 0)


class BackendSelectionTests(unittest.TestCase):
    def test_backend_is_chosen_by_a_fixed_name_and_defaults_to_hindsight(self):
        self.assertEqual(parse_hindsight(None).backend, "hindsight")
        self.assertEqual(parse_hindsight({"backend": "hindsight"}).backend, "hindsight")
        for name in ("mem0", "Hindsight", "skirnir_router.hindsight:HindsightBackend", 1, None):
            with self.subTest(name=name):
                self.assertTrue(failed(parse_hindsight({"backend": name})))
        self.assertEqual(config.validate_schema({"router": {"hindsight": {"backend": "hindsight"}}}), [])
        unknown = memory_backend.open_backend(replace(HindsightConfig(), backend="mem0"), "")
        self.assertEqual((failed(unknown), unknown.status), (True, 400))


# The Hindsight-compatible gateway and the memory REST API are public wire contracts: these answers were recorded from
# the router before the backend interface (main at router 0.39) and must stay identical byte for byte.
OPERATION = "a1bee8fc-3c76-5c3b-a3e6-f6962293a2d2"
STATUS_HEAD = (f'{{"operation_id": "{OPERATION}", "state": "%s", "attempts": 1, "created_at": "2026-01-01T00:00:00+00:00", '
               '"updated_at": "2026-01-01T00:00:00+00:00", ')
STATUS_TAIL = '"last_error": null, "retry_rounds": 0, "upstream_attempts": 0, "action": "retain"'
EXPECTED = {
    "version": (200, '{"gateway": "skirnir", "capabilities": ["recall", "bounded-retain", "operation-status"]}'),
    "gateway_recall": (200, '{"results": [{"id": "fact-1", "text": "Synthetic project uses versioned events.", "metadata": {"kind": '
                            '"confirmed_information", "confirmed": "true", "origin_client": "client-b"}}, {"id": "fact-2", "text": '
                            '"Synthetic second statement.", "metadata": {}}], "trusted": false, "memory_state": "available", "trimmed": 0, '
                            '"duplicates": 1, "limited_by": "results"}'),
    "rest_recall": (200, '{"items": [{"source": "fact-1", "text": "Synthetic project uses versioned events.", "provenance": {"kind": '
                         '"confirmed_information", "confirmed": "true", "origin_client": "client-b"}, "created_at": "2026-01-02T03:04:05+00:00"}, '
                         '{"source": "fact-2", "text": "Synthetic second statement.", "updated_at": "2026-01-03T00:00:00Z"}], "trusted": false, '
                         '"memory_state": "available", "trimmed": 0, "duplicates": 1, "limited_by": "results"}'),
    "gateway_recall_missing": (200, '{"results": [], "trusted": false, "memory_state": "bank_missing", "trimmed": 0, "duplicates": 0, '
                                    '"limited_by": null}'),
    "gateway_recall_down": (503, '{"error": {"message": "memory unavailable", "type": "memory_unavailable"}, "memory_state": "unavailable"}'),
    "gateway_retain": (200, f'{{"success": true, "bank_id": "shared-project", "items_count": 1, "operation_id": "{OPERATION}", '
                            '"async": true, "durable": true}'),
    "gateway_status_accepted": (200, STATUS_HEAD % "accepted" + '"upstream_status": "processing", ' + STATUS_TAIL + ', "status": "processing"}'),
    "rest_status_accepted": (200, STATUS_HEAD % "accepted" + '"upstream_status": "processing", ' + STATUS_TAIL + "}"),
    "gateway_status_other": (200, STATUS_HEAD % "accepted" + '"upstream_status": "weird-new-state", ' + STATUS_TAIL + ', "status": "processing"}'),
    "gateway_status_gone": (404, '{"error": {"message": "upstream operation no longer available", "type": "session_error"}}'),
    "gateway_status_completed": (200, STATUS_HEAD % "completed" + STATUS_TAIL + ', "status": "completed"}'),
    "rest_list": (200, f'{{"items": [{{"id": "unit-1", "type": "world", "text": "Synthetic listed fact.", "document_id": "{OPERATION}", '
                       '"source_memory_ids": ["a", "b"], "provenance": {"kind": "assistant_derivation", "confirmed": "false", "origin_client": '
                       '"client-a"}, "date": "2026-01-01T00:00:00Z"}, {"id": "unit-3", "type": null, "text": "No type here.", "document_id": '
                       'null, "source_memory_ids": [], "provenance": {}, "updated_at": "2026-01-05T00:00:00Z"}], "withheld": [{"id": "unit-2", '
                       '"document_id": null}], "total": 4, "offset": 0, "limit": 10, "trusted": false}'),
}
# What the router sends to Hindsight is a wire contract too (design/hindsight-memory.md, "Contract and compatibility").
RECALL_REQUEST = {"query": "Synthetic", "budget": "low", "max_tokens": 4096, "include": {"entities": None, "source_facts": {"max_tokens": 0}}}


class GatewayByteCompatibilityTests(delivery.Harness):
    async def answer(self, name, response):
        status, body = EXPECTED[name]
        self.assertEqual((response.status, response.headers.get("Content-Type"), await response.read()),
                         (status, "application/json; charset=utf-8", body.encode()), name)

    async def test_gateway_and_rest_answers_are_unchanged_byte_for_byte(self):
        auth = {"Authorization": "Bearer client-a"}
        gateway = memory_gateway.GATEWAY + "/v1/default/banks/" + shared.SHARED
        recall = {"query": "Synthetic"}
        await self.answer("version", await self.client.get(memory_gateway.GATEWAY + "/v1/version", headers=auth))
        self.results = [
            {"id": "fact-1", "text": "Synthetic project uses versioned events.", "type": "world",
             "metadata": {"kind": "confirmed_information", "origin_client": "client-b", "confirmed": "true", "source": "client"},
             "created_at": "2026-01-02T03:04:05+00:00", "scores": {"reranker": 0.9}},
            {"id": "obs-1", "text": "Versioned events are used.", "type": "observation", "source_fact_ids": ["fact-1"]},
            {"id": "fact-2", "text": "Synthetic second statement.", "type": "experience", "updated_at": "2026-01-03T00:00:00Z"},
            "not-an-object", {"id": 5, "text": "bad id"}]
        await self.answer("gateway_recall", await self.client.post(gateway + "/memories/recall", json=recall, headers=auth))
        self.assertEqual(self.calls[-1][1], RECALL_REQUEST)
        await self.answer("rest_recall", await self.client.post(memory.PREFIX + "/recall", json={"project": shared.SHARED, "query": "Synthetic"},
                                                                headers=auth))
        self.failure_status = 404
        await self.answer("gateway_recall_missing", await self.client.post(gateway + "/memories/recall", json=recall, headers=auth))
        self.failure_status = 500
        await self.answer("gateway_recall_down", await self.client.post(gateway + "/memories/recall", json=recall, headers=auth))
        self.failure_status = 0
        await self.answer("gateway_retain", await self.client.post(gateway + "/memories", headers=auth, json={
            "async": True, "items": [{"content": "Synthetic gateway fact", "document_id": "doc-1"}]}))
        await memory_outbox.step(self.service)
        sent = copy.deepcopy(self.operations[OPERATION])
        sent["items"][0]["metadata"].pop("observed_at")
        self.assertEqual(sent, {"async": True, "operation_id": OPERATION, "items": [{
            "content": "Synthetic gateway fact", "document_id": OPERATION, "context": "Unverified assistant response; not confirmed facts",
            "metadata": {"kind": "assistant_derivation", "origin_client": "client-a", "version": OPERATION,
                         "source_refs": json.dumps(["provider-document:" + hashlib.sha256(b"doc-1").hexdigest()]),
                         "confirmed": "false", "source": "assistant"}}]})
        self.operation_state = "processing"
        await self.answer("gateway_status_accepted", await self.client.get(gateway + "/operations/" + OPERATION, headers=auth))
        await self.answer("rest_status_accepted", await self.client.get(memory.PREFIX + "/operations/" + OPERATION, headers=auth))
        self.operation_state = "weird-new-state"   # a state the router does not know is passed on as reported
        await self.answer("gateway_status_other", await self.client.get(gateway + "/operations/" + OPERATION, headers=auth))
        self.operation_state = "not_found"
        await self.answer("gateway_status_gone", await self.client.get(gateway + "/operations/" + OPERATION, headers=auth))
        self.store.release(OPERATION)
        self.operation_state = "completed"
        await memory_outbox.step(self.service)
        await self.answer("gateway_status_completed", await self.client.get(gateway + "/operations/" + OPERATION, headers=auth))
        self.list_units = [
            {"id": "unit-1", "fact_type": "world", "text": "Synthetic listed fact.", "document_id": OPERATION, "source_memory_ids": ["a", 3, "b"],
             "metadata": {"kind": "assistant_derivation", "origin_client": "client-a", "confirmed": "false"}, "date": "2026-01-01T00:00:00Z"},
            {"id": "unit-2", "fact_type": "observation", "text": "Contact person@example.com", "document_id": None},
            {"id": "unit-3", "text": "No type here.", "updated_at": "2026-01-05T00:00:00Z"}, "junk"]
        await self.answer("rest_list", await self.client.post(memory.PREFIX + "/list", json={"project": shared.SHARED, "limit": 10}, headers=auth))


if __name__ == "__main__":
    unittest.main(verbosity=2)
