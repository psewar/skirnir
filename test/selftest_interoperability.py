#!/usr/bin/env python3
"""Synthetic gateway, policy, durable acceptance, provenance and handoff contracts."""
import asyncio
import copy
import hashlib
import hmac
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from selftest_memory import MemoryHarness, PRIVATE, PROJECT, policy, realistic_fact, trimmed_count
from selftest_context import ManagedHarness, history, row
from skirnir_router import config, context_ops, memory, memory_gateway, memory_outbox, session_api, session_exchange, state
from skirnir_router.context_config import ContextConfig
from skirnir_router.context_manager import encoded, estimate, project
from skirnir_router.memory_config import CLIENT_MEMORY_FIELDS, PROJECT_FIELDS, parse_client_memory, parse_hindsight
from skirnir_router.shared_config import parse_scopes
from skirnir_router.outcome import conflict, failed, unavailable
from skirnir_router.reliability_store import OPERATION_NOT_FOUND
from skirnir_router.session_store import SessionStore
from skirnir_router.session_types import UNAVAILABLE

SHARED = "shared-project"


class PolicyTests(unittest.TestCase):
    def test_new_config_strict_optional_and_private_defaults(self):
        self.assertFalse(parse_hindsight(None).outbox_enabled)
        self.assertFalse(parse_hindsight(None).gateway_enabled)
        for data in ({"outbox_enabled": 1}, {"outbox_attempts": 0}, {"outbox_lease_s": 1}, {"recall_min_score": float("nan")}):
            with self.subTest(data=data):
                self.assertTrue(failed(parse_hindsight(data)))
        for data in ({"a": {"data_class": "unknown"}}, {"bad/alias": {"data_class": "personal"}}, {"a": {"data_class": "personal", "bank": "chosen"}}):
            with self.subTest(data=data):
                self.assertTrue(failed(parse_scopes(data, ["personal"])))
        for data in ({"owner": "client", "strategy": "auto"}, {"strategy": "hybrid"}, {"strategy": "manual", "auto_retain": True}):
            with self.subTest(data=data):
                self.assertTrue(failed(parse_client_memory(data, ["personal"])))
        self.assertEqual(parse_client_memory(None, ["personal"])["strategy"], "legacy")

    def test_recall_with_names_other_readable_projects_of_the_client(self):
        def spec(recall_with, shared_read=True):
            return {"owner": "client", "strategy": "manual", "projects": {
                "own": {"data_class": "personal", "read": True, "recall_with": recall_with},
                "team": {"data_class": "personal", "read": shared_read, "scope": "example-team"}}}
        ok = parse_client_memory(spec(["team"]), ["personal"])
        self.assertEqual(ok["projects"]["own"]["recall_with"], ("team",))
        self.assertEqual(ok["projects"]["team"]["recall_with"], ())
        for bad in (spec(["own"]), spec(["unknown"]), spec(["team"], shared_read=False), spec("team"), spec(["team", "team"]),
                    spec(["a", "b", "c", "d", "e"])):
            with self.subTest(bad=bad["projects"]["own"]["recall_with"]):
                self.assertTrue(failed(parse_client_memory(bad, ["personal"])))

    def test_config_schema_accepts_every_field_the_parser_accepts(self):
        # 0.8.9 accepted recall_with in parse_client_memory, but the schema check refused it, so no config could load it
        memory_spec = {**dict.fromkeys(CLIENT_MEMORY_FIELDS, 1), "projects": {"own": dict.fromkeys(PROJECT_FIELDS, 1)}}
        raw = {"router": {"client_auth": {"clients": {"example-client": {"memory": memory_spec}}}}}
        self.assertEqual(config.validate_schema(raw), [])
        memory_spec["projects"]["own"]["recal_with"] = 1
        self.assertEqual(len(config.validate_schema(raw)), 1)

    def test_provenance_cannot_promote_unconfirmed_knowledge(self):
        self.assertTrue(failed(memory_gateway.provenance({"kind": "confirmed_information"}, "client-a", confirmed=False)))
        self.assertTrue(failed(memory_gateway.provenance({"origin_client": "client-b"}, "client-a", confirmed=True)))
        origin = memory_gateway.provenance({"source_refs": ["repo:example.py"], "supersedes": "source-1"}, "client-a", confirmed=False)
        self.assertEqual(origin["origin_client"], "client-a")
        self.assertEqual(origin["confirmed"], "false")

    def test_completed_tool_reduction_preserves_pair_and_original(self):
        context = history()
        call = {"id": "call-example", "function": {"name": "example", "arguments": {}}}
        context["messages"][2] = row(3, "assistant", "", tool_calls=[call])
        context["messages"][3] = row(4, "tool", "Synthetic tool data " * 2000, tool_call_id="call-example", tool_name="example")
        original = copy.deepcopy(context)
        self.assertEqual(project(context, {}, 7000, ContextConfig(tail_messages=2), "example").status, 413)
        cfg = ContextConfig(tail_messages=2, reduce_tool_results=True, tool_result_bytes=256)
        messages, version = project(context, {}, 7000, cfg, "example")
        self.assertEqual(context, original)
        result = next(m for m in messages if m.get("tool_call_id") == "call-example")
        self.assertIn("original event sequence 4", result["content"])
        self.assertIn("unverified", result["content"])
        self.assertTrue(any(m.get("tool_calls") == [call] for m in messages))
        self.assertIn(4, version["payload"]["provenance"]["reduced_tool_sequences"])
        self.assertLessEqual(estimate({}, messages), 7000)

    def test_reduction_does_not_touch_unpaired_or_protected_tool_result(self):
        context = history()
        context["messages"][3] = row(4, "tool", "x" * 30000, tool_call_id="orphan")
        self.assertEqual(project(context, {}, 6000, ContextConfig(tail_messages=2, reduce_tool_results=True), "example").status, 413)
        context["messages"][2] = row(3, "assistant", "", tool_calls=[{"id": "orphan", "function": {"name": "example", "arguments": {}}}])
        context["protected_sequences"] = [4]
        self.assertEqual(project(context, {}, 6000, ContextConfig(tail_messages=2, reduce_tool_results=True), "example").status, 413)


class SharedGatewayTests(MemoryHarness):
    def shared(self):
        state.CFG.memory_scopes = {"example-team": {"data_class": "personal", "allow_cloud": False}}
        for name in ("client-a", "client-b"):
            spec = policy()
            spec["projects"][SHARED] = {**spec["projects"][PROJECT], "scope": "example-team", "delete": True}
            state.CFG.client_auth["clients"][name]["memory"] = spec
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, gateway_enabled=True)

    async def api(self, path, data=None, token="client-a", method="post"):
        return await getattr(self.client, method)(path, json=data, headers={"Authorization": "Bearer " + token} if token else {})

    async def durable(self):
        self.service.worker.cancel()
        await asyncio.gather(self.service.worker, return_exceptions=True)
        self.service.worker = None
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, outbox_enabled=True)
        self.jobs = {}
        store = AsyncMock(spec=SessionStore)

        async def put(operation, scope, payload, fingerprint, capacity):
            ident = str(operation)
            if ident in self.jobs:
                if self.jobs[ident]["fingerprint"] != fingerprint:
                    return conflict("event_id already used for different memory content")
                return self.jobs[ident]
            if sum(r["state"] in ("pending", "processing") for r in self.jobs.values()) >= capacity:
                return unavailable("memory outbox full")
            self.jobs[ident] = {"operation_id": ident, "owner": scope["principal"], "project": scope["project"], "scope": scope,
                "payload": payload, "fingerprint": fingerprint, "attempts": 0, "state": "pending", "lease": str(uuid4()),
                "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"}
            return self.jobs[ident]

        async def get(owner, operation):
            value = self.jobs.get(str(operation))
            if value is None or value["owner"] != owner:
                return OPERATION_NOT_FOUND
            return value

        async def claim(*args):
            for job in self.jobs.values():
                if job["state"] in ("pending", "delete_pending"):
                    job["state"] = "processing"
                    job["attempts"] += 1
                    return copy.deepcopy(job)
            return None

        async def finish(operation, lease, status, delay, error=None):
            self.jobs[str(operation)]["state"] = status

        async def find(operation):
            value = self.jobs.get(str(operation))
            return OPERATION_NOT_FOUND if value is None else value

        async def delete(owner, operation, scope):
            value = self.jobs.get(str(operation))
            if value is None or value["owner"] != owner:
                return OPERATION_NOT_FOUND
            value.update(state="delete_pending", delete_requested=True, scope=scope)
            return None
        store.outbox_put.side_effect = put
        store.outbox_get.side_effect = get
        store.outbox_find.side_effect = find
        store.outbox_delete.side_effect = delete
        store.outbox_claim.side_effect = claim
        store.outbox_finish.side_effect = finish
        self.service.outbox = store
        return store

    async def test_shared_reads_private_ids_and_third_client_denial(self):
        self.shared()
        for token in ("client-a", "client-b"):
            response = await self.api(memory.PREFIX + "/recall", {"project": SHARED, "query": "Synthetic question"}, token)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["items"][0]["source"], "source-1")
        self.assertEqual(self.calls[0][0], self.calls[1][0])
        response = await self.api(memory.PREFIX + "/recall", {"project": SHARED, "query": "Synthetic"}, "client-owned")
        self.assertEqual(response.status, 403)
        a = memory.scope_for_principal("client-a", PROJECT, read=True)
        b = memory.scope_for_principal("client-b", PROJECT, read=True)
        self.assertNotEqual(self.service.bank(a), self.service.bank(b))
        expected = "skirnir-" + hmac.new(self.service.key, encoded(["v1", "client-a", PROJECT, "personal"]), hashlib.sha256).hexdigest()
        self.assertEqual(self.service.bank(a), expected)

    async def test_independent_write_read_revoke_and_shared_operation_namespaces(self):
        self.shared()
        specs = state.CFG.client_auth["clients"]
        specs["client-a"]["memory"]["projects"][SHARED]["write"] = False
        data = {"project": SHARED, "event_id": "source-1", "content": "Synthetic fact", "confirmed": True}
        self.assertEqual((await self.api(memory.PREFIX + "/retain", data)).status, 403)
        self.assertEqual((await self.api(memory.PREFIX + "/recall", {"project": SHARED, "query": "Synthetic"})).status, 200)
        specs["client-b"]["memory"]["projects"][SHARED]["read"] = False
        self.assertEqual((await self.api(memory.PREFIX + "/retain", data, "client-b")).status, 202)
        self.assertEqual((await self.api(memory.PREFIX + "/recall", {"project": SHARED, "query": "Synthetic"}, "client-b")).status, 403)
        a = memory.scope_for_principal("client-a", SHARED)
        b = memory.scope_for_principal("client-b", SHARED)
        self.assertNotEqual(self.service.operation(a, "same"), self.service.operation(b, "same"))
        specs["client-a"]["memory"]["projects"][SHARED]["read"] = False
        self.assertEqual((await self.api(memory.PREFIX + "/recall", {"project": SHARED, "query": "Synthetic"})).status, 403)

    async def test_native_hermes_gateway_shares_bank_without_double_injection(self):
        self.shared()
        spec = policy(owner="client")
        spec.update(strategy="manual")
        spec["projects"][SHARED] = {"scope": "example-team", "data_class": "personal", "read": True, "write": True}
        state.CFG.client_auth["clients"]["client-owned"]["memory"] = spec
        base = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED
        response = await self.api(base + "/memories/recall", {"query": "Synthetic", "budget": "mid", "types": ["observation"], "max_tokens": 4096}, "client-owned")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["results"][0]["id"], "source-1")
        other = memory.scope_for_principal("client-a", SHARED, read=True)
        self.assertEqual(self.calls[-1][0], self.service.bank(other))
        before = len(self.calls)
        response = await self.infer(enabled=False, token="client-owned")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.calls), before)
        self.assertEqual((await self.infer(token="client-owned")).status, 403)
        self.assertEqual((await self.api(base + "/memories/recall", {"query": "Synthetic"}, token="")).status, 401)
        version = await self.api(memory_gateway.GATEWAY + "/v1/version", method="get")
        self.assertNotIn("version", await version.json())

    def owned_with_shared(self):
        """client-owned reads PROJECT (private) with SHARED (example-team) via recall_with; both banks with their own facts."""
        self.shared()
        spec = policy(owner="client")
        spec.update(strategy="manual")
        spec["projects"][PROJECT]["recall_with"] = (SHARED,)
        spec["projects"][SHARED] = {"scope": "example-team", "data_class": "personal", "read": True, "write": True}
        state.CFG.client_auth["clients"]["client-owned"]["memory"] = spec
        own = self.service.bank(memory.scope_for_principal("client-owned", PROJECT, read=True, explicit=True))
        team = self.service.bank(memory.scope_for_principal("client-owned", SHARED, read=True, explicit=True))
        self.results_by_bank = {own: [{"id": "p1", "text": "Private fact one."}, {"id": "p2", "text": "Private fact two."}],
                                team: [{"id": "s1", "text": "Shared team fact."}, {"id": "s2", "text": "Private fact one."}]}
        return own, team, memory_gateway.GATEWAY + "/v1/default/banks/" + PROJECT + "/memories/recall"

    async def test_gateway_recall_with_reads_shared_scope_too(self):
        own, team, url = self.owned_with_shared()
        response = await self.api(url, {"query": "Synthetic"}, "client-owned")
        self.assertEqual(response.status, 200)
        body = await response.json()
        # rank by rank: own 1, shared 1, own 2; the shared copy of "Private fact one." says nothing new
        self.assertEqual([r["text"] for r in body["results"]], ["Private fact one.", f"[{SHARED}] Shared team fact.", "Private fact two."])
        self.assertEqual(body["results"][1]["metadata"].get("memory_project"), SHARED)
        self.assertNotIn("memory_project", body["results"][0]["metadata"])
        self.assertEqual(body["also"], [{"project": SHARED, "memory_state": "available", "items": 2}])
        self.assertEqual(body["duplicates"], 1)
        self.assertEqual({c[0] for c in self.calls}, {own, team})
        # REST recall: same merge, the project as a field
        rest = await self.api(memory.PREFIX + "/recall", {"project": PROJECT, "query": "Synthetic"}, "client-owned")
        items = (await rest.json())["items"]
        self.assertEqual([(i["source"], i.get("project")) for i in items], [("p1", None), ("s1", SHARED), ("p2", None)])

    async def test_recall_with_shared_failure_keeps_the_private_answer(self):
        own, team, url = self.owned_with_shared()
        self.results_by_bank[team] = 503
        body = await (await self.api(url, {"query": "Synthetic"}, "client-owned")).json()
        self.assertEqual([r["text"] for r in body["results"]], ["Private fact one.", "Private fact two."])
        self.assertEqual(body["memory_state"], "available")
        self.assertTrue(body["also"][0]["memory_state"].startswith("unavailable"), body["also"])
        # the shared grant is withdrawn: reported as denied, never read
        state.CFG.client_auth["clients"]["client-owned"]["memory"]["projects"][SHARED]["read"] = False
        self.calls.clear()
        body = await (await self.api(url, {"query": "Synthetic"}, "client-owned")).json()
        self.assertEqual(body["also"], [{"project": SHARED, "memory_state": "denied", "items": 0}])
        self.assertEqual({c[0] for c in self.calls}, {own})
        # the private bank fails: the answer fails, whatever the shared one has
        state.CFG.client_auth["clients"]["client-owned"]["memory"]["projects"][SHARED]["read"] = True
        self.results_by_bank[own], self.results_by_bank[team] = 503, [{"id": "s1", "text": "Shared team fact."}]
        self.assertEqual((await self.api(url, {"query": "Synthetic"}, "client-owned")).status, 503)

    async def test_gateway_recall_budget_only_lowers_the_server_byte_cap(self):
        self.shared()
        spec = policy(owner="client")
        spec.update(strategy="manual")
        spec["projects"][SHARED] = {"scope": "example-team", "data_class": "personal", "read": True, "write": True}
        state.CFG.client_auth["clients"]["client-owned"]["memory"] = spec
        self.results = [realistic_fact("source-1", "Synthetic project uses versioned events.")]
        base = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories/recall"
        before = trimmed_count()
        # Without max_tokens the server byte cap alone applies.
        response = await self.api(base, {"query": "Synthetic"}, "client-owned")
        self.assertEqual([r["id"] for r in (await response.json())["results"]], ["source-1"])
        self.assertEqual(trimmed_count(), before)
        # As in Hindsight, the client's max_tokens counts fact text only (here 40 bytes, at most 40 tokens), not the
        # provenance and JSON around it: 100 tokens hold this fact, although the whole block is far larger.
        value = await (await self.api(base, {"query": "Synthetic", "max_tokens": 100}, "client-owned")).json()
        self.assertEqual(([r["id"] for r in value["results"]], value["limited_by"]), (["source-1"], "results"))
        value = await (await self.api(base, {"query": "Synthetic", "max_tokens": 10}, "client-owned")).json()
        self.assertEqual((value["results"], value["limited_by"]), ([], "client"))
        self.assertEqual(trimmed_count(), before + 1)
        self.assertEqual((await self.api(base, {"query": "Synthetic", "max_tokens": 0}, "client-owned")).status, 400)

    async def test_hermes_default_budget_no_longer_caps_a_larger_server_limit(self):
        # Hermes sends max_tokens 4096 by default. Applied to the whole block as bytes it silently held the recall at
        # 4 KiB whatever the server allowed; applied to fact text it only lowers when the client really wants less.
        self.shared()
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, recall_max_bytes=16384)
        self.results = [realistic_fact(f"source-{i}", f"Synthetic project fact number {i} with a few more words.") for i in range(60)]
        base = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories/recall"
        value = await (await self.api(base, {"query": "Synthetic", "max_tokens": 4096})).json()
        self.assertEqual(value["limited_by"], "bytes")
        self.assertGreater(len(memory.packed([{"source": r["id"], "text": r["text"], "provenance": r["metadata"]}
                                              for r in value["results"]]).encode()), 8192)

    async def test_gateway_never_becomes_an_arbitrary_proxy(self):
        self.shared()
        prefix = memory_gateway.GATEWAY + "/v1/default/banks/"
        for alias in ("unconfigured", self.service.bank(memory.scope_for_principal("client-a", SHARED))):
            response = await self.api(prefix + alias + "/memories/recall", {"query": "Synthetic"})
            self.assertEqual(response.status, 403)
        response = await self.api(prefix + SHARED + "/memories/reflect", {"query": "Synthetic"})
        self.assertEqual(response.status, 404)
        response = await self.api(prefix + SHARED + "/memories/recall", {"query": "Synthetic", "tags": ["other"]})
        self.assertEqual(response.status, 400)

    async def test_gateway_recall_carries_the_memory_state(self):
        self.shared()
        base = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories/recall"
        value = await (await self.api(base, {"query": "Synthetic"})).json()
        self.assertEqual((value["results"][0]["id"], value["memory_state"], value["trimmed"], value["duplicates"]),
                         ("source-1", "available", 0, 0))
        # Unreachable or oversized memory is a 503 with its state, never an empty result list (Hermes would
        # otherwise report "No relevant memories found" for a memory it never reached).
        self.large_response = True
        response = await self.api(base, {"query": "Synthetic"})
        self.assertEqual(response.status, 503)
        value = await response.json()
        self.assertEqual(value["memory_state"], "unavailable:protocol")
        self.assertNotIn("results", value)

    async def test_private_addresses_through_gateway_and_durable_outbox(self):
        # The outbox filters again when it delivers: a local-only shared bank keeps addresses on both checks, and the
        # gateway answers a filtered query with 403 and its state.
        self.shared()
        await self.durable()
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED
        data = {"async": True, "items": [{"content": f"The synthetic service runs on {PRIVATE}.", "context": f"Synthetic host {PRIVATE}"}]}
        response = await self.api(path + "/memories", data)
        self.assertEqual(response.status, 403)
        # The client context is filtered with the same scope setting as the content.
        response = await self.api(path + "/memories", {"async": True, "items": [{"content": "Synthetic plain fact", "context": f"Synthetic host {PRIVATE}"}]})
        self.assertEqual(response.status, 403)
        response = await self.api(path + "/memories/recall", {"query": f"What runs on {PRIVATE}?"})
        self.assertEqual((response.status, (await response.json())["memory_state"]), (403, "query_filtered"))
        state.CFG.client_auth["clients"]["client-a"]["memory"]["projects"][SHARED]["allow_private_addresses"] = True
        response = await self.api(path + "/memories", data)
        self.assertEqual(response.status, 200)
        operation = (await response.json())["operation_id"]
        await memory_outbox.step(self.service)
        retained = self.operations[operation]["items"][0]
        self.assertEqual(retained["content"], f"The synthetic service runs on {PRIVATE}.")
        self.assertTrue(retained["context"].endswith(f"client context: Synthetic host {PRIVATE}"))
        self.assertEqual((await self.api(path + "/memories/recall", {"query": f"What runs on {PRIVATE}?"})).status, 200)

    async def test_gateway_retain_rejection_names_its_reason(self):
        self.shared()
        await self.durable()
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories"
        item = {"content": "Synthetic knowledge"}
        for option, value in (("tags", ["synthetic-tag"]), ("entities", [{"text": "Synthetic"}]), ("strategy", "synthetic"),
                              ("resolve_entities", False)):
            with self.subTest(option=option):
                response = await self.api(path, {"async": True, "items": [{**item, option: value}]})
                self.assertEqual(response.status, 400)
                error = (await response.json())["error"]
                self.assertEqual(error["message"], "invalid memory gateway request")
                self.assertEqual(error["reason"], "unsupported gateway retain option: " + option)
        self.assertEqual(self.jobs, {})

    async def test_gateway_retain_passes_client_context_and_timestamp_untrusted(self):
        self.shared()
        await self.durable()
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories"
        item = {"content": "Synthetic team moved the review to Friday", "context": "Synthetic project decision",
                "timestamp": "2026-03-14T09:30:00+01:00", "metadata": {"confirmed": "true"}}
        response = await self.api(path, {"async": True, "items": [item]})
        self.assertEqual(response.status, 200)
        operation = (await response.json())["operation_id"]
        self.assertFalse(self.jobs[operation]["payload"]["confirmed"])
        await memory_outbox.step(self.service)
        retained = self.operations[operation]["items"][0]
        # The fixed notice stays first: a client context can describe the content, never confirm it.
        self.assertEqual(retained["context"], "Unverified assistant response; not confirmed facts; client context: Synthetic project decision")
        self.assertEqual(retained["timestamp"], "2026-03-14T09:30:00+01:00")
        self.assertEqual((retained["metadata"]["confirmed"], retained["metadata"]["kind"]), ("false", "assistant_derivation"))
        # UTC written as Z reaches Hindsight in one canonical form.
        response = await self.api(path, {"async": True, "items": [{"content": "Synthetic second fact", "timestamp": "2026-03-14T08:30:00.250Z"}]})
        operation = (await response.json())["operation_id"]
        await memory_outbox.step(self.service)
        retained = self.operations[operation]["items"][0]
        self.assertEqual(retained["timestamp"], "2026-03-14T08:30:00.250000+00:00")
        self.assertEqual(retained["context"], "Unverified assistant response; not confirmed facts")

    async def test_gateway_retain_without_context_and_timestamp_keeps_the_stored_shape(self):
        # Rows admitted before this field support carry only content/confirmed/provenance. New rows without the
        # options keep that exact shape and fingerprint, so a replay of an old retain still finds its operation.
        self.shared()
        await self.durable()
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories"
        # The official SDK sends both fields as null when unset (Hindsight: no timestamp means "now").
        for number, options in enumerate(({}, {"context": None, "timestamp": None}, {"context": ""})):
            with self.subTest(options=options):
                self.jobs.clear()
                item = {"content": f"Synthetic plain fact {number}", **options}
                operation = (await (await self.api(path, {"async": True, "items": [item]})).json())["operation_id"]
                job = self.jobs[operation]
                self.assertEqual(set(job["payload"]), {"content", "confirmed", "provenance"})
                self.assertEqual(job["fingerprint"], memory_outbox.digest(job["payload"]))
                await memory_outbox.step(self.service)
                retained = self.operations[operation]["items"][0]
                self.assertNotIn("timestamp", retained)
                self.assertEqual(retained["context"], "Unverified assistant response; not confirmed facts")

    async def test_gateway_retain_replay_with_a_new_timestamp_is_the_same_operation(self):
        # Hermes stamps every send with the current time; a replayed document must not become a conflict or a duplicate.
        self.shared()
        await self.durable()
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories"
        item = {"content": "Synthetic replayed transcript", "document_id": "provider-document-2", "context": "Synthetic label"}
        first = await self.api(path, {"async": True, "items": [{**item, "timestamp": "2026-03-14T09:30:00+01:00"}]})
        operation = (await first.json())["operation_id"]
        replay = await self.api(path, {"async": True, "items": [{**item, "timestamp": "2026-03-14T10:45:00+01:00", "context": "Other label"}]})
        self.assertEqual(replay.status, 200)
        self.assertEqual((await replay.json())["operation_id"], operation)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[operation]["payload"]["timestamp"], "2026-03-14T09:30:00+01:00")

    async def test_gateway_retain_rejects_invalid_timestamp_and_context(self):
        self.shared()
        await self.durable()
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories"
        item = {"content": "Synthetic knowledge"}
        for value in ("2026-02-30T10:00:00+00:00", "2026-13-01T00:00:00Z", "2026-03-14T24:00:00Z", "2026-03-14T09:60:00Z",
                      "2026-03-14T09:30:00+15:00", "0000-01-01T00:00:00Z", "2026-03-14", "2026-03-14T09:30:00", "unset",
                      "yesterday", "2026-03-14T09:30:00+01:00\n", 1773480600, ""):
            with self.subTest(timestamp=value):
                response = await self.api(path, {"async": True, "items": [{**item, "timestamp": value}]})
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"]["reason"],
                                 "gateway retain timestamp must be an ISO 8601 date and time with a UTC offset")
        for value in ("x" * 257, "Synthetic\nlabel", ["Synthetic"], "   "):
            with self.subTest(context=value):
                response = await self.api(path, {"async": True, "items": [{**item, "context": value}]})
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"]["reason"], "context must be a nonempty string of at most 256 characters")
        # The context passes the same sensitive-data filter as the content.
        response = await self.api(path, {"async": True, "items": [{**item, "context": "Synthetic note for someone@example.com"}]})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.jobs, {})

    async def test_gateway_retain_context_is_filtered_again_before_delivery(self):
        self.shared()
        await self.durable()
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories"
        response = await self.api(path, {"async": True, "items": [{"content": "Synthetic fact", "context": "Synthetic codename label"}]})
        operation = (await response.json())["operation_id"]
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, deny_terms=("codename",))
        await memory_outbox.step(self.service)
        self.assertEqual(self.jobs[operation]["state"], "revoked")
        self.assertEqual(self.operations, {})

    async def test_persistent_202_is_durable_and_database_failure_is_not_accepted(self):
        self.shared()
        store = await self.durable()
        data = {"project": SHARED, "event_id": "source-1", "content": "Synthetic fact", "confirmed": True}
        response = await self.api(memory.PREFIX + "/retain", data)
        self.assertEqual(response.status, 202)
        value = await response.json()
        self.assertTrue(value["durable"])
        self.assertIn(value["operation_id"], self.jobs)
        duplicate = await self.api(memory.PREFIX + "/retain", data)
        self.assertEqual((await duplicate.json())["operation_id"], value["operation_id"])
        self.assertEqual(len(self.jobs), 1)
        store.outbox_put.side_effect = lambda *args, **kwargs: UNAVAILABLE
        self.assertEqual((await self.api(memory.PREFIX + "/retain", {**data, "event_id": "second"})).status, 503)

    async def test_outbox_retry_idempotency_dead_letter_and_revoke(self):
        self.shared()
        store = await self.durable()
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, outbox_attempts=2)
        data = {"project": SHARED, "event_id": "source-1", "content": "Synthetic fact", "confirmed": True}
        operation = (await (await self.api(memory.PREFIX + "/retain", data)).json())["operation_id"]
        self.retain_failures = 20
        await memory_outbox.step(self.service)
        self.assertEqual(self.jobs[operation]["state"], "pending")
        await memory_outbox.step(self.service)
        self.assertEqual(self.jobs[operation]["state"], "dead")
        self.assertEqual(len(self.operations), 1)
        self.assertEqual(self.jobs[operation]["attempts"], 2)
        second = (await (await self.api(memory.PREFIX + "/retain", {**data, "event_id": "second"})).json())["operation_id"]
        state.CFG.client_auth["clients"]["client-a"]["memory"]["projects"][SHARED]["write"] = False
        before = len(self.calls)
        await memory_outbox.step(self.service)
        self.assertEqual(self.jobs[second]["state"], "revoked")
        self.assertEqual(len(self.calls), before)
        store.outbox_finish.assert_awaited()

    async def test_namespace_key_changes_never_redirect_persisted_jobs(self):
        self.shared()
        await self.durable()
        data = {"project": SHARED, "event_id": "one", "content": "Synthetic fact", "confirmed": True}
        operation = (await (await self.api(memory.PREFIX + "/retain", data)).json())["operation_id"]
        self.service.key = b"different-synthetic-namespace-key-32-bytes"
        await memory_outbox.step(self.service)
        self.assertEqual(self.jobs[operation]["state"], "revoked")
        self.assertEqual(self.calls, [])

    async def test_hermes_repeated_compaction_retains_are_deduplicated(self):
        self.shared()
        await self.durable()
        path = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories"
        data = {"async": True, "items": [{"content": "Synthetic unverified transcript", "document_id": "provider-document-1", "metadata": {"confirmed": "true"}}]}
        first = await self.api(path, data)
        self.assertEqual(first.status, 200)
        operation = (await first.json())["operation_id"]
        for _ in range(3):
            replay = await self.api(path, data)
            self.assertEqual((await replay.json())["operation_id"], operation)
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(self.jobs[operation]["payload"]["confirmed"])
        await memory_outbox.step(self.service)
        retained = self.operations[operation]["items"][0]
        self.assertEqual(retained["metadata"]["confirmed"], "false")
        self.assertEqual(retained["metadata"]["kind"], "assistant_derivation")
        self.assertEqual(retained["metadata"]["origin_client"], "client-a")
        response = await self.api(path, {**data, "items": [{**data["items"][0], "update_mode": "append"}]})
        self.assertEqual(response.status, 400)

    async def test_operation_status_is_owner_scoped_and_readable_after_ack(self):
        self.shared()
        await self.durable()
        self.service.client.operation = AsyncMock(return_value={"status": "completed"})
        data = {"project": SHARED, "event_id": "one", "content": "Synthetic fact", "confirmed": True}
        operation = (await (await self.api(memory.PREFIX + "/retain", data)).json())["operation_id"]
        state.CFG.client_auth["clients"]["client-b"]["memory"]["projects"][SHARED]["delete"] = False   # reader, not curator
        self.assertEqual((await self.api(memory.PREFIX + "/operations/" + operation, token="client-b", method="get")).status, 404)
        await memory_outbox.step(self.service)
        response = await self.api(memory.PREFIX + "/operations/" + operation, method="get")
        value = await response.json()
        self.assertEqual(value["upstream_status"], "completed")
        self.assertNotIn("payload", value)
        self.assertNotIn("scope", value)

    async def test_curator_deletes_foreign_operations_only_in_its_shared_scope(self):
        self.shared()
        await self.durable()
        self.service.client.operation = AsyncMock(return_value={"status": "completed"})
        shared_op = (await (await self.api(memory.PREFIX + "/retain", {"project": SHARED, "event_id": "one",
                                                                       "content": "Synthetic fact", "confirmed": True})).json())["operation_id"]
        private_op = (await (await self.api(memory.PREFIX + "/retain", {"project": PROJECT, "event_id": "two",
                                                                        "content": "Synthetic note", "confirmed": True})).json())["operation_id"]
        await memory_outbox.step(self.service)
        await memory_outbox.step(self.service)
        path = memory.PREFIX + "/operations/"
        # client-b holds read and delete on the shared scope: it curates, also what client-a wrote.
        self.assertEqual((await self.api(path + shared_op, token="client-b", method="get")).status, 200)
        self.assertEqual((await self.api(path + shared_op + "/retry", {"retry_id": "r1"}, token="client-b")).status, 404)
        self.assertEqual((await self.api(path + private_op + "/delete", {}, token="client-b")).status, 404)
        response = await self.api(path + shared_op + "/delete", {}, token="client-b")
        self.assertEqual(response.status, 202, await response.text())
        job = self.jobs[shared_op]
        self.assertEqual((job["owner"], job["state"], job["scope"]["principal"]), ("client-a", "delete_pending", "client-b"))
        self.assertEqual(job["scope"]["__bank"], self.service.bank(memory.scope_for_principal("client-a", SHARED, explicit=True)))
        before = len(self.deleted_documents)
        await memory_outbox.step(self.service)
        self.assertEqual(self.deleted_documents[before:], [(job["scope"]["__bank"], shared_op)])
        self.assertEqual(self.jobs[shared_op]["state"], "deleted")
        # Without delete it is a reader again: foreign operations stay invisible.
        second = (await (await self.api(memory.PREFIX + "/retain", {"project": SHARED, "event_id": "three",
                                                                    "content": "Synthetic other fact", "confirmed": True})).json())["operation_id"]
        state.CFG.client_auth["clients"]["client-b"]["memory"]["projects"][SHARED]["delete"] = False
        self.assertEqual((await self.api(path + second + "/delete", {}, token="client-b")).status, 404)
        self.assertEqual((await self.api(path + second + "/delete", {}, token="client-owned")).status, 404)
        self.assertEqual(self.jobs[second]["state"], "pending")

    async def test_curator_lists_the_shared_scope_and_withholds_filtered_units(self):
        self.shared()
        mail = "@".join(("synthetic", "example.invalid"))   # assembled, so the publication guard stays quiet
        self.list_units = [
            {"id": "unit-1", "fact_type": "world", "text": "Synthetic fact one.", "document_id": "doc-1", "date": "2026-01-02",
             "metadata": {"kind": "assistant_derivation", "origin_client": "client-a", "unlisted": "dropped"}},
            {"id": "unit-2", "fact_type": "observation", "text": "Synthetic summary.", "document_id": None,
             "source_memory_ids": ["unit-1"]},
            {"id": "unit-3", "fact_type": "world", "text": "Reach " + mail, "document_id": "doc-3"}]
        response = await self.api(memory.PREFIX + "/list", {"project": SHARED, "limit": 10}, token="client-b")
        self.assertEqual(response.status, 200, await response.text())
        value = await response.json()
        self.assertEqual([i["id"] for i in value["items"]], ["unit-1", "unit-2"])
        self.assertEqual(value["items"][0]["document_id"], "doc-1")
        self.assertEqual(value["items"][0]["provenance"], {"kind": "assistant_derivation", "origin_client": "client-a"})
        self.assertEqual(value["items"][1]["source_memory_ids"], ["unit-1"])
        self.assertEqual(value["withheld"], [{"id": "unit-3", "document_id": "doc-3"}])
        self.assertEqual((value["total"], value["trusted"]), (3, False))
        bank, query = self.list_queries[-1]
        self.assertEqual(bank, self.service.bank(memory.scope_for_principal("client-b", SHARED, read=True, explicit=True)))
        self.assertEqual((query["limit"], query["offset"]), ("10", "0"))
        # Only curators, only shared scopes, bounded pages.
        for data, token, status in (({"project": SHARED, "limit": 0}, "client-b", 400),
                                    ({"project": SHARED, "type": "opinion"}, "client-b", 400),
                                    ({"project": PROJECT}, "client-b", 403),
                                    ({"project": SHARED}, "client-owned", 403)):
            self.assertEqual((await self.api(memory.PREFIX + "/list", data, token=token)).status, status, (data, token))
        state.CFG.client_auth["clients"]["client-b"]["memory"]["projects"][SHARED]["delete"] = False
        self.assertEqual((await self.api(memory.PREFIX + "/list", {"project": SHARED}, token="client-b")).status, 403)

    async def test_strategies_auto_hybrid_manual_off_and_default_legacy(self):
        spec = state.CFG.client_auth["clients"]["client-a"]["memory"]
        spec.update(strategy="hybrid", default_project=PROJECT)
        self.assertEqual((await self.infer(enabled=False)).status, 200)
        self.assertTrue(self.calls)
        self.assertEqual(self.operations, {})
        spec.update(strategy="auto", auto_retain=True)
        self.assertEqual((await self.infer(enabled=False, extra={"routing": {"request_id": "auto-turn"}})).status, 200)
        await self.service.queue.join()
        self.assertEqual(len(self.operations), 1)
        spec.update(strategy="manual", auto_retain=False)
        self.assertEqual((await self.infer()).status, 403)
        self.assertEqual((await self.call(data={"project": PROJECT, "query": "Synthetic"})).status, 200)
        spec["strategy"] = "off"
        self.assertEqual((await self.call()).status, 403)

    async def test_manual_provider_context_bypasses_cache_and_observation(self):
        from skirnir_router import ops, proxy
        for token in ("client-a", "client-b"):
            state.CFG.client_auth["clients"][token]["memory"]["strategy"] = "manual"
            response = await self.infer(enabled=False, token=token, extra={"routing": {"idempotency_key": "same-synthetic-key"}})
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.upstream_bodies), 2)
        ops.maybe_shadow.assert_not_called()
        proxy.ausweich_beobachtung.begin.assert_not_called()

    async def test_revoke_while_waiting_for_http_slot_prevents_dispatch(self):
        self.shared()
        await self.durable()
        await self.api(memory.PREFIX + "/retain", {"project": SHARED, "event_id": "one", "content": "Synthetic fact", "confirmed": True})
        slots = self.service.client.slots
        for _ in range(self.service.cfg.max_inflight):
            await slots.acquire()
        task = asyncio.create_task(memory_outbox.step(self.service))
        await asyncio.sleep(0)
        state.CFG.client_auth["clients"]["client-a"]["memory"]["projects"][SHARED]["write"] = False
        for _ in range(self.service.cfg.max_inflight):
            slots.release()
        await task
        self.assertEqual(self.calls, [])
        self.assertEqual(next(iter(self.jobs.values()))["state"], "revoked")

    async def test_relevance_threshold_and_provenance_are_untrusted(self):
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, recall_min_score=0.5)
        self.results = [{"id": "relevant", "text": "Synthetic relevant fact", "scores": {"reranker": .9},
                         "metadata": {"kind": "assistant_derivation", "confirmed": "false", "origin_client": "client-a", "supersedes": "older-source"}},
                        {"id": "irrelevant", "text": "Synthetic other topic", "scores": {"reranker": .1}},
                        {"id": "unscored", "text": "Synthetic unscored"}]
        value = await (await self.call()).json()
        self.assertEqual([v["source"] for v in value["items"]], ["relevant"])
        self.assertFalse(value["trusted"])
        self.assertEqual(value["items"][0]["provenance"]["confirmed"], "false")


    async def test_official_hindsight_sdk_recall_retain_and_operation_status(self):
        try:
            from hindsight_client import Hindsight
        except ImportError:
            self.skipTest("install test/requirements-hindsight.txt for the official SDK contract test")
        self.shared()
        await self.durable()
        client = Hindsight(base_url=str(self.client.make_url(memory_gateway.GATEWAY)), api_key="client-a")
        try:
            recalled = await client.arecall(bank_id=SHARED, query="Synthetic question", budget="low", max_tokens=512)
            self.assertEqual(recalled.results[0].id, "source-1")
            retained = await client.aretain_batch(bank_id=SHARED, items=[{"content": "Synthetic SDK knowledge"}],
                                                 document_id="synthetic-sdk-document", retain_async=True)
            self.assertTrue(retained.success)
            self.assertEqual(retained.bank_id, SHARED)
            self.assertEqual(retained.items_count, 1)
            self.assertIn(str(retained.operation_id), self.jobs)
            # As the Hermes plugin sends it: an aware event time and a short label.
            dated = await client.aretain_batch(bank_id=SHARED, items=[{"content": "Synthetic dated SDK knowledge", "context": "Synthetic label",
                                                                       "timestamp": datetime(2026, 3, 14, 9, 30, tzinfo=timezone(timedelta(hours=1)))}],
                                               document_id="synthetic-dated-document", retain_async=True)
            payload = self.jobs[str(dated.operation_id)]["payload"]
            self.assertEqual((payload["context"], payload["timestamp"]), ("Synthetic label", "2026-03-14T09:30:00+01:00"))
            self.service.client.operation = AsyncMock(return_value={"status": "completed"})
            await memory_outbox.step(self.service)
            status = await client.operations.get_operation_status(bank_id=SHARED, operation_id=str(retained.operation_id))
            self.assertEqual(status.status, "completed")
            # An oversized upstream answer reaches the SDK as a failure, not as an empty result list.
            self.large_response = True
            from hindsight_client_api.exceptions import ApiException
            with self.assertRaises(ApiException):   # genuine exception: the SDK's own way to report HTTP 503
                await client.arecall(bank_id=SHARED, query="Synthetic question", budget="low", max_tokens=512)
        finally:
            await client.aclose()


class SessionExchangeTests(ManagedHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        state.CFG.session_store = replace(state.CFG.session_store, handoff_enabled=True)
        self.key_env = patch.dict(os.environ, {"SKIRNIR_HANDOFF_KEY": "synthetic-handoff-key-32-bytes-minimum"})
        self.key_env.start()
        self.context.update(history())
        self.context["checkpoint"] = {"version": 1, "payload": {"schema_version": 1, "goals": ["Synthetic goal"],
            "open_steps": ["Synthetic pending work"], "artifacts": ["repo:example.py"], "last_result": "Synthetic output"}}
        self.session["metadata"] = {"handoff": {"repository": "https://git.example.invalid/example/project", "branch": "example", "commit": "a" * 40}}
        self.store.import_session.return_value = ({"id": str(uuid4()), "project": PROJECT, "mode": "client-managed"}, True)
        self.store.discover.return_value = [{"id": str(self.sid), "project": PROJECT, "status": "active", "mode": "skirnir-managed", "data_class": "personal", "updated_at": "2026-01-01T00:00:00+00:00"}]

    async def asyncTearDown(self):
        self.key_env.stop()
        await super().asyncTearDown()

    async def export(self):
        response = await self.client.get(session_api.PREFIX + "/" + str(self.sid) + "/handoff", headers={"Authorization": "Bearer client-a"})
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def import_(self, package, token="client-a", **extra):
        return await self.client.post(session_api.PREFIX + "/import", json={"package": package, "project": PROJECT, "event_id": "import-1", **extra},
                                      headers={"Authorization": "Bearer " + token})

    async def test_discovery_uses_authenticated_owner_filters_and_cursor(self):
        response = await self.client.get(session_api.PREFIX + "?project=" + PROJECT + "&status=active&updated_since=2026-01-01T00:00:00Z&limit=2",
                                         headers={"Authorization": "Bearer client-a"})
        value = await response.json()
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(value["next_before"])
        self.assertEqual(self.store.discover.call_args.args[0], "client-a")
        self.assertNotIn("metadata", value["items"][0])
        for cursor in ("bad", "W10=", "e30="):
            response = await self.client.get(session_api.PREFIX + "?before=" + cursor, headers={"Authorization": "Bearer client-a"})
            self.assertEqual(response.status, 400)
        response = await self.client.get(session_api.PREFIX + "?project=forbidden", headers={"Authorization": "Bearer client-a"})
        self.assertEqual(response.status, 403)

    async def test_handoff_export_validation_import_and_no_execution(self):
        package = await self.export()
        self.assertFalse(package["resumable_execution"])
        self.assertEqual(package["work"]["open_steps"], ["Synthetic pending work"])
        self.assertEqual(package["work"]["references"]["branch"], "example")
        response = await self.import_(package)
        self.assertEqual(response.status, 201, await response.text())
        args = self.store.import_session.call_args.args
        self.assertEqual(args[0], "client-a")
        self.assertTrue(all(m["role"] == "user" and "Untrusted handoff" in m["content"] for m in args[-2]))
        self.assertFalse(args[-3]["resumable_execution"])
        self.assertEqual(args[-1]["goals"], ["Synthetic goal"])

    async def test_cross_client_import_requires_explicit_live_grants(self):
        package = await self.export()
        self.assertEqual((await self.import_(package, "client-b")).status, 403)
        state.CFG.client_auth["clients"]["client-b"]["session_handoff_from"] = ["client-a"]
        response = await self.import_(package, "client-b")
        self.assertEqual(response.status, 201, await response.text())
        state.CFG.client_auth["clients"]["client-a"]["session_projects"] = []
        self.assertEqual((await self.import_(package, "client-b")).status, 403)

    async def test_tamper_expiry_secret_filter_and_reference_checks(self):
        package = await self.export()
        bad = copy.deepcopy(package)
        bad["resumable_execution"] = True
        self.assertEqual((await self.import_(bad)).status, 400)
        self.store.import_session.assert_not_awaited()
        unsigned = {**package, "expires_at": 0}
        unsigned.pop("signature")
        unsigned["signature"] = session_exchange.signature(unsigned)
        self.assertEqual((await self.import_(unsigned)).status, 400)
        for value in ("/private/example.txt", "../example.txt", "https://git.example.invalid/?secret=synthetic"):
            with self.subTest(value=value):
                self.assertTrue(failed(session_exchange.reference(value)))
        state.CFG.hindsight = replace(state.CFG.hindsight, deny_terms=("SYNTHETIC_DENIED",))
        self.context["messages"].append(row(21, content="SYNTHETIC_DENIED"))
        response = await self.client.get(session_api.PREFIX + "/" + str(self.sid) + "/handoff", headers={"Authorization": "Bearer client-a"})
        self.assertEqual(response.status, 403)

    async def test_handoff_classification_cannot_be_downgraded(self):
        self.session["data_class"] = "secret"
        package = await self.export()
        response = await self.import_(package)
        self.assertEqual(response.status, 201, await response.text())
        self.assertEqual(self.store.import_session.call_args.args[4], "secret")

    async def test_dynamic_recall_budget_protects_checkpoint_and_recent_input(self):
        from skirnir_router.managed import Turn
        from skirnir_router.request import Routing
        cfg = replace(state.CFG.context_manager, optimize_recall=True)
        req = Routing.fresh()
        req.memory_items = [{"source": "synthetic-source", "text": "Synthetic recall " * 2000}]
        req.memory_context = {"role": "user", "content": memory.packed(req.memory_items)}
        req.data_class = "personal"
        turn = Turn(None, req, self.store, "client-a", self.sid, uuid4(), cfg)
        turn.check = lambda: None
        turn.context = copy.deepcopy(self.context)
        for entry in turn.context["messages"]:
            if entry["seq"] in (18, 19):
                entry["payload"]["content"] = "Synthetic recent message"
        body = {"messages": [], "options": {"num_predict": 1024}}
        await turn.target(body, self.tier, 4096)
        self.assertFalse(any("Synthetic recall" in m.get("content", "") for m in body["messages"]))
        self.assertTrue(any("Synthetic pending work" in m.get("content", "") for m in body["messages"]))
        self.assertTrue(any("Current question" in m.get("content", "") for m in body["messages"]))
        self.assertLessEqual(estimate(body, body["messages"]), 4096 - 1024 - cfg.safety_tokens)

    async def test_latest_checkpoint_explicit_and_content_free_admin_status(self):
        response = await self.client.get(session_api.PREFIX + "/" + str(self.sid) + "/checkpoints/latest", headers={"Authorization": "Bearer client-a"})
        self.assertEqual((await response.json())["checkpoint"]["version"], 1)
        await context_ops.poll(self.client.server.app)
        value = encoded(context_ops.snapshot()).decode()
        self.assertNotIn(str(self.sid), value)
        self.assertNotIn("Synthetic goal", value)
        self.assertIn("session_store", value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
