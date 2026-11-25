#!/usr/bin/env python3
"""Self-test (TM-05): the cloud checks see the final payload, including facts the router added from memory.

Memory recall inserts a user-role data message (memory.packed: a notice plus the facts as JSON with non-ASCII characters
escaped) before the last user message. A request routed to a cloud tier is checked after that insertion: the credential
scan (cloud.request_block_reason) and, for a client with the explicit privacy exception (`privacy_check: true`), the
content check (privacy.check). This test checks
  - the order in proxy._prepare: memory first, then the cloud block reason on the augmented body;
  - the credential scan finds a secret inside an inserted fact;
  - the privacy rules see an inserted fact as text, also when it contains non-ASCII characters that the JSON escaping
    turns into \\uXXXX (a term "Müller" in a fact must keep the request local);
  - the classifier receives the inserted facts as readable text;
  - with the privacy exception, a request whose user text and facts pass is sent with the facts, as the final payload.
Since router 0.42.6 also tool results and every other text a provider receives:
  - the credential scan reads content parts (a list on the native chat path, which OpenAI-style providers get as is),
    the arguments of tool calls in the history and tool results, not only string contents;
  - with the privacy exception the provider receives the final payload with memory facts and tool results, the house
    context redacted (a door sensor in a Home Assistant live context), an unrecognised tool result unchanged by default
    (ADR-0125) and the client's own body untouched; with `unrecognised_tool_results: keep_local` nothing is sent.
Synthetic values only; no network, classifier or provider."""
import asyncio
import inspect
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import cloud, memory, privacy, proxy, state  # noqa: E402
from skirnir_router import config as config_mod  # noqa: E402
from skirnir_router.shape import OllamaShape  # noqa: E402

CFG = privacy.parse_privacy_config({"enabled": True, "terms": ["Müller", "Lea"]})
QUESTION = "Wann ist der Termin?"


def augmented(*facts):
    """A chat body after memory recall: the data message from memory.packed before the last user message."""
    items = [{"source": f"source-{i}", "text": text} for i, text in enumerate(facts, 1)]
    return {"model": "assist:latest", "stream": False, "messages": [
        {"role": "system", "content": "Synthetic system instruction"},
        {"role": "user", "content": memory.packed(items)},
        {"role": "user", "content": QUESTION}]}


class FinalPayloadTests(unittest.TestCase):
    def setUp(self):
        self.classified = []
        self.original_classify = privacy.classify

        async def classify(texts, cfg):
            self.classified.append(list(texts))
            return 0.05, 0.4   # neutral: only the rules decide in these tests
        privacy.classify = classify

    def tearDown(self):
        privacy.classify = self.original_classify

    def test_memory_is_inserted_before_the_cloud_block_reason_is_taken(self):
        # _prepare inserts memory, then orders the cloud tiers; _cloud_ordered takes the block reason on that body.
        source = inspect.getsource(proxy._prepare)
        self.assertLess(source.index("memory.prepare("), source.index("_cloud_ordered("),
                        "the cloud checks must run on the augmented body")
        self.assertIn("cloud.request_block_reason(req, body", inspect.getsource(proxy._cloud_ordered))

    def test_credential_scan_finds_a_secret_in_an_inserted_fact(self):
        secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2   # synthetic OpenAI-style key shape, assembled for the publication guard
        body = augmented("The project key is " + secret)
        hit = cloud.scan_secrets(body)
        self.assertIsNotNone(hit, "the secret inside the memory data message is found")
        self.assertIsNone(cloud.scan_secrets(augmented("The project uses versioned events.")))

    def test_credential_scan_reads_every_text_a_provider_receives(self):
        secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2   # synthetic key shape, assembled for the publication guard
        def with_call(arguments):
            """An augmented body whose history holds an assistant tool call before the question."""
            body = augmented("The project uses versioned events.")
            body["messages"].insert(-1, {"role": "assistant", "content": "",
                                         "tool_calls": [{"function": {"name": "store", "arguments": arguments}}]})
            return body
        places = {
            "tool result": {"messages": [{"role": "user", "content": QUESTION}, {"role": "tool", "content": "token " + secret}]},
            "content parts": {"messages": [{"role": "user", "content": [{"type": "text", "text": "token " + secret}]}]},
            "tool call arguments (object)": with_call({"token": secret}),
            "tool call arguments (JSON text)": with_call(json.dumps({"token": secret})),
        }
        for place, body in places.items():
            with self.subTest(place=place):
                self.assertIsNotNone(cloud.scan_secrets(body), f"a secret in the {place} is found")
                clean = json.loads(json.dumps(body).replace(secret, "versioned-events"))
                self.assertIsNone(cloud.scan_secrets(clean), f"nothing found without it ({place})")
        # instrument: the native chat path hands content parts to an OpenAI-style provider as they are
        sent = cloud._oa_messages(places["content parts"], "/api/chat")
        self.assertEqual(sent[0]["content"], places["content parts"]["messages"][0]["content"])
        for odd in ({"messages": [{"role": "assistant", "tool_calls": {"function": "x"}}]},
                    {"messages": [{"role": "assistant", "tool_calls": [None, {"function": None}, {"function": {"arguments": 5}}]}]},
                    {"messages": [{"role": "user", "content": [None, {"text": 5}, "plain"]}]}):
            self.assertIsNone(cloud.scan_secrets(odd), "malformed parts and calls are skipped, not an error")

    def test_privacy_rules_see_inserted_facts_also_with_non_ascii_characters(self):
        self.assertIn("\\u00fc", augmented("Termin bei Frau Müller am Freitag")["messages"][1]["content"],
                      "instrument: the data message escapes non-ASCII characters")
        verdict = asyncio.run(privacy.check(augmented("Termin bei Frau Müller am Freitag"), CFG))
        self.assertEqual((verdict.allowed, verdict.by), (False, "rules:terms"), "a term in an inserted fact keeps the request local")
        verdict = asyncio.run(privacy.check(augmented("Lea has an appointment on Friday"), CFG))
        self.assertEqual((verdict.allowed, verdict.by), (False, "rules:terms"))

    def test_classifier_receives_inserted_facts_as_readable_text(self):
        verdict = asyncio.run(privacy.check(augmented("Der Wochenmarkt öffnet um acht Uhr."), CFG))
        self.assertTrue(verdict.allowed)
        texts = self.classified[-1]
        self.assertIn("Der Wochenmarkt öffnet um acht Uhr.", texts, texts)
        self.assertIn(QUESTION, texts)

    def test_a_look_alike_message_is_plain_text(self):
        # A client can send a user message that merely starts like the notice: no decoding, no error, still checked.
        for content in (memory.NOTICE + "{broken", memory.NOTICE + '{"text": "x"}', memory.NOTICE + '[1, {"text": 2}]'):
            with self.subTest(content=content[-20:]):
                self.assertEqual(memory.unpacked_texts(content), [])
                self.assertIn(content, privacy.user_texts({"messages": [{"role": "user", "content": content}]}))

    def test_privacy_exception_sends_the_final_payload_with_the_facts(self):
        sent = []
        original_run = cloud.run

        async def fake_run(node, path, out, model, stream):
            sent.append(out)
            return {"p_tok": 1, "c_tok": 1}, {"model": model, "message": {"role": "assistant", "content": "Freitag."}, "done": True}
        cloud.run = fake_run
        previous_cfg = state.CFG
        try:
            cfg = config_mod.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
            cfg.cloud = {**cfg.cloud, "privacy_check": CFG}
            state.CFG = cfg
            relay = object.__new__(proxy._Relay)
            relay.req = type("Q", (), {"cloud_privacy": True, "data_class_effective": "personal", "request_id": "t1", "explicit": False})()
            relay.path, relay.out, relay.stream = "/api/chat", augmented("Der Wochenmarkt öffnet um acht Uhr."), False
            relay.model, relay.exposed = "openai:example-model", "assist:latest"
            relay.node = type("N", (), {"name": "cloud:openai", "is_cloud": True, "spec": {"kind": "openai", "trust": "cloud"}})()
            relay.shape, relay.headers, relay.memory_capture, relay.info = OllamaShape(), {}, None, None
            response = asyncio.run(proxy._Relay._via_cloud(relay))
            self.assertEqual(response.status, 200)
            self.assertEqual(len(sent), 1)
            payload = json.dumps(sent[0])
            self.assertIn("Untrusted retrieved memory", payload, "the provider receives the inserted facts")
            self.assertIn(QUESTION, payload)
            sent.clear()
            relay.out = augmented("Termin bei Frau Müller am Freitag")
            response = asyncio.run(proxy._Relay._via_cloud(relay))
            self.assertEqual(sent, [], "a fact with a term keeps the request local: no provider call")
            self.assertEqual(json.loads(response.body)["message"]["content"], CFG.refusal)
        finally:
            cloud.run = original_run
            state.CFG = previous_cfg

    def send(self, body, privacy_cfg):
        """Run _via_cloud for a client with the privacy exception; (response, payloads the provider received)."""
        sent = []
        original_run, previous_cfg = cloud.run, state.CFG

        async def fake_run(node, path, out, model, stream):
            sent.append(out)
            return {"p_tok": 1, "c_tok": 1}, {"model": model, "message": {"role": "assistant", "content": "22 Grad."}, "done": True}
        cloud.run = fake_run
        try:
            cfg = config_mod.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
            cfg.cloud = {**cfg.cloud, "privacy_check": privacy_cfg}
            state.CFG = cfg
            relay = object.__new__(proxy._Relay)
            relay.req = type("Q", (), {"cloud_privacy": True, "data_class_effective": "personal", "request_id": "t2", "explicit": False})()
            relay.path, relay.out, relay.stream = "/api/chat", body, False
            relay.model, relay.exposed = "openai:example-model", "assist:latest"
            relay.node = type("N", (), {"name": "cloud:openai", "is_cloud": True, "spec": {"kind": "openai", "trust": "cloud"}})()
            relay.shape, relay.headers, relay.memory_capture, relay.info = OllamaShape(), {}, None, None
            return asyncio.run(proxy._Relay._via_cloud(relay)), sent
        finally:
            cloud.run, state.CFG = original_run, previous_cfg

    def test_final_payload_with_tool_results(self):
        live = ("Live Context:\n- names: Wohnzimmer Temperatur\n  domain: sensor\n  state: '22.0'\n"
                "- names: Haustuer Kontakt\n  domain: binary_sensor\n  state: 'off'\n  attributes:\n    device_class: door\n")
        body = augmented("Der Wochenmarkt öffnet um acht Uhr.")
        body["messages"] += [
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "GetLiveContext", "arguments": {}}}]},
            {"role": "tool", "tool_name": "GetLiveContext", "content": json.dumps({"success": True, "result": live})},
            {"role": "tool", "tool_name": "GetTime", "content": "Es ist 14:05."}]
        original = json.dumps(body)
        response, sent = self.send(body, CFG)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(sent), 1, "one provider call with the final payload")
        payload = json.dumps(sent[0], ensure_ascii=False)
        self.assertIn("Untrusted retrieved memory", payload, "memory facts are part of it")
        self.assertIn("Wohnzimmer Temperatur", payload, "the tool result is part of it")
        self.assertNotIn("Haustuer", payload, "a door sensor of the house context is redacted before it leaves")
        self.assertIn("Es ist 14:05.", payload, "an unrecognised tool result passes unchanged by default (ADR-0125)")
        self.assertEqual(json.dumps(body), original, "the client's body is not changed; the redacted copy is sent")
        self.assertIn("Haustuer", original, "instrument: the door sensor was in the request")

        keep_local = privacy.parse_privacy_config({"enabled": True, "terms": ["Müller", "Lea"],
                                                   "unrecognised_tool_results": "keep_local"})
        response, sent = self.send(json.loads(original), keep_local)
        self.assertEqual(sent, [], "keep_local: an unrecognised tool result keeps the request local, nothing is sent")
        self.assertEqual(json.loads(response.body)["message"]["content"], keep_local.refusal)


if __name__ == "__main__":
    unittest.main(verbosity=1)
