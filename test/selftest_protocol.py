#!/usr/bin/env python3
"""Self-test (ADR-0108): the router-agent protocol (docs/protocol.md, docs/protocol/) matches its implementations.

The specification is only useful while it describes what the code does. This test keeps the schemas, the router and the
test fake agent in step; agent/protocol_conformance_test.go does the same for the Go agent.

  1. The schemas use only the keywords the validator implements, every property is described, every example conforms.
  2. docs/protocol/index.json names the same frame codes as router/skirnir_router/tunnel.py and test/fake_agent.py.
  3. Every control message the router code builds (a dict with a constant "t") is a documented message with only
     documented fields, and every field the router reads from an agent message is documented.
  4. What "required" claims holds in the router: a message without a required field is refused or dropped, a message
     with only the required fields is accepted, and the documented examples are accepted.
  5. End to end: the real router (control app in-process) and test/fake_agent.py (subprocess) talk through a relay that
     validates every message in both directions strictly against the schemas: login, approval with the configuration
     bundle, heartbeat and its answer, the OLLAMA report, and a request stream (REQ, RESP, DATA, END).

Runs without network beyond loopback, without a router process and without an agent binary."""
import ast
import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
ROUTER_PKG = os.path.join(ROOT, "router", "skirnir_router")
sys.path.insert(0, os.path.join(ROOT, "router"))
sys.path.insert(0, HERE)
import protocol_schema  # noqa: E402
from skirnir_router import app, config, nodes, openai_node, poll, registry, state, tunnel  # noqa: E402

EXAMPLE = os.path.join(ROOT, "router", "config.example.yaml")
SCHEMAS = protocol_schema.load_schemas()
with open(os.path.join(protocol_schema.SCHEMA_DIR, "index.json"), encoding="utf-8") as _f:
    INDEX = json.load(_f)
CODES = {name: f["code"] for name, f in INDEX["frames"].items()}
BY_CODE = {f["code"]: (name, f) for name, f in INDEX["frames"].items()}
FRAME_SCHEMA = {"HB": "heartbeat.schema.json", "HBACK": "heartbeat-ack.schema.json", "OLLAMA": "ollama-state.schema.json",
                "POWER": "power.schema.json", "RESP": "response-head.schema.json"}


def props(schema_file, pointer=""):
    target = protocol_schema.resolve(SCHEMAS, schema_file, pointer or "#")
    return set((target[1].get("properties") or {}).keys())


def strict(name, value):
    return protocol_schema.validate(name, value, strict=True, schemas=SCHEMAS)


def setup_router_state(tmp):
    state.CFG = config.Config(EXAMPLE, use_overrides=False)
    state.REG = registry.NodeRegistry(os.path.join(tmp, "nodes.json")).load()
    state.NODES, state.PENDING = {}, {}
    registry._ENROLL.clear()


class Schemas(unittest.TestCase):
    def test_schemas_are_well_formed_and_examples_conform(self):
        self.assertEqual(protocol_schema.schema_problems(SCHEMAS), [])

    def test_validator_refuses_what_it_does_not_implement(self):
        bad = {"x.schema.json": {"title": "x", "description": "x", "examples": [{}], "type": "object", "oneOf": []}}
        self.assertTrue(any("unsupported keyword 'oneOf'" in p for p in protocol_schema.schema_problems(bad)))

    def test_strict_mode_refuses_unknown_fields_lenient_mode_ignores_them(self):
        msg = {"state": "suspending", "extra": 1}
        self.assertEqual(protocol_schema.validate("power.schema.json", msg, schemas=SCHEMAS), [])
        self.assertTrue(strict("power.schema.json", msg))

    def test_index_names_existing_schemas(self):
        for name, m in INDEX["text_messages"].items():
            self.assertIn(m["schema"], SCHEMAS, name)
            t = SCHEMAS[m["schema"]]["properties"]["t"]
            self.assertTrue(t.get("const") == name or name in (t.get("enum") or []), f"{m['schema']} does not declare t={name}")
        for name, schema in FRAME_SCHEMA.items():
            self.assertIn(schema, INDEX["frames"][name]["payload"])


class FrameCodes(unittest.TestCase):
    def test_router_constants_match_the_index(self):
        router = {n: getattr(tunnel, "TUN_" + n) for n in CODES}
        self.assertEqual(router, CODES)

    def test_fake_agent_constants_match_the_index(self):
        with open(os.path.join(HERE, "fake_agent.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple) and isinstance(node.value, ast.Tuple):
                names = [t.id for t in node.targets[0].elts if isinstance(t, ast.Name)]
                values = [v.value for v in node.value.elts if isinstance(v, ast.Constant)]
                if "REQ" in names:
                    found = dict(zip(names, values, strict=True))
        self.assertTrue(found, "fake_agent.py: frame constants not found")
        self.assertEqual({n: CODES[n] for n in found}, found)

    def test_index_limits_match_the_router(self):
        self.assertEqual(registry.MAX_HELLO_BYTES, INDEX["limits"]["hello_max_bytes"])
        self.assertEqual(nodes.PUSH_STALE_S, INDEX["limits"]["ollama_fresh_s"])


def router_sources():
    for name in sorted(os.listdir(ROUTER_PKG)):
        if name.endswith(".py"):
            path = os.path.join(ROUTER_PKG, name)
            with open(path, encoding="utf-8") as f:
                yield name, ast.parse(f.read(), filename=path)


def is_get(call, var):
    """call is var.get(...) or var[...]-like access on the name var."""
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "get"
            and isinstance(call.func.value, ast.Name) and call.func.value.id == var)


def keys_read(func, var):
    """String keys a function reads from the dict named var: var.get("k"), var["k"], and var.get(k) for k in ("a", ...)."""
    out = set()
    for node in ast.walk(func):
        # var.get(k) or var[k] inside "for k in ("a", "b")": that loop's constants are keys read from var
        gens = node.generators if isinstance(node, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)) else []
        for g in gens + ([node] if isinstance(node, ast.For) else []):
            if not (isinstance(g.target, ast.Name) and isinstance(g.iter, ast.Tuple)):
                continue
            uses = any((is_get(n, var) and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == g.target.id)
                       or (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == var
                           and isinstance(n.slice, ast.Name) and n.slice.id == g.target.id)
                       for n in ast.walk(node))
            if uses:
                out.update(e.value for e in g.iter.elts if isinstance(e, ast.Constant))
        if is_get(node, var) and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                out.add(a.value)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == var
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            out.add(node.slice.value)
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
                and any(isinstance(op, ast.In) for op in node.ops) and isinstance(node.comparators[0], ast.Name)
                and node.comparators[0].id == var):
            out.add(node.left.value)
    return out


# Which variable in which router function holds which agent message (file, function, variable, schema, pointer).
READERS = [
    ("poll.py", "apply_heartbeat", "b", "heartbeat.schema.json", ""),
    ("poll.py", "note_game_memory", "b", "heartbeat.schema.json", ""),
    ("poll.py", "note_game_memory", "g", "heartbeat.schema.json", "#/$defs/game"),
    ("poll.py", "note_game_memory", "m", "heartbeat.schema.json", "#/$defs/memory"),
    ("poll.py", "route_health", "r", "heartbeat.schema.json", "#/$defs/route_health"),
    ("poll.py", "apply_ollama_push", "msg", "ollama-state.schema.json", ""),
    ("poll.py", "apply_ollama_push", "blocked", "ollama-state.schema.json", "#/$defs/upstream_blocked"),
    ("poll.py", "apply_power", "msg", "power.schema.json", ""),
    ("registry.py", "receive_hello", "hello", "hello.schema.json", ""),
    ("registry.py", "handle_tunnel_v2", "hello", "hello.schema.json", ""),
    ("tunnel.py", "response_head", "h", "response-head.schema.json", ""),
    ("agentupdate.py", "note_report", "report", "heartbeat.schema.json", "#/$defs/update_report"),
    ("ollamaupdate.py", "note_report", "report", "heartbeat.schema.json", "#/$defs/update_report"),
]


class RouterCodeMatchesTheSpec(unittest.TestCase):
    def test_every_control_message_the_router_builds_is_documented(self):
        seen = set()
        for file, tree in router_sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
                if "t" not in keys:
                    continue
                t = node.values[keys.index("t")]
                if not (isinstance(t, ast.Constant) and isinstance(t.value, str)):
                    continue   # a timestamp field named t (journal entries), or an f-string checked at runtime below
                where = f"{file}:{node.lineno}"
                self.assertIn(t.value, INDEX["text_messages"], f"{where}: message t={t.value!r} is not in docs/protocol")
                schema = INDEX["text_messages"][t.value]["schema"]
                self.assertNotIn(None, keys, f"{where}: message with a computed key")
                self.assertLessEqual(set(keys), props(schema), f"{where}: fields not in {schema}")
                self.assertLessEqual(set(SCHEMAS[schema].get("required", [])), set(keys), f"{where}: required field missing")
                seen.add(t.value)
        # every router-to-agent message except the f-string upstream-* orders is built as a literal somewhere
        expected = {n for n, m in INDEX["text_messages"].items() if m["direction"] == "router-to-agent"} - {"upstream-hold", "upstream-release"}
        self.assertEqual(seen & expected, expected)

    def test_every_field_the_router_reads_from_the_agent_is_documented(self):
        trees = dict(router_sources())
        for file, func_name, var, schema, pointer in READERS:
            funcs = [n for n in ast.walk(trees[file]) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name]
            self.assertEqual(len(funcs), 1, f"{file}: function {func_name} not found (update READERS)")
            read = keys_read(funcs[0], var)
            self.assertTrue(read, f"{file}:{func_name}: reads nothing from {var} (update READERS)")
            self.assertLessEqual(read, props(schema, pointer), f"{file}:{func_name} reads undocumented fields of {schema}{pointer}")

    def test_every_fact_the_router_reads_is_documented(self):
        read = set()
        for _, tree in router_sources():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                        and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    continue
                v = node.func.value
                named = isinstance(v, ast.Name) and v.id == "facts"
                inline = (isinstance(v, ast.BoolOp) and isinstance(v.values[0], ast.Call) and v.values[0].args
                          and isinstance(v.values[0].args[0], ast.Constant) and v.values[0].args[0].value == "facts")
                if named or inline:
                    read.add(node.args[0].value)
        self.assertGreaterEqual(len(read), 10, read)
        self.assertLessEqual(read, props("facts.schema.json"))


class FakeWS:
    """Just enough of aiohttp's WebSocketResponse for registry.receive_hello."""

    def __init__(self, text):
        self.text = text

    async def receive(self):
        return type("Msg", (), {"type": web.WSMsgType.TEXT, "data": self.text})()


def signed_hello(nonce, node="synthetic-node", **extra):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    msg = {"t": "hello", "pubkey": base64.b64encode(pub).decode(), "sig": base64.b64encode(key.sign(nonce + node.encode() + pub)).decode()}
    if node:
        msg["node"] = node
    msg.update(extra)
    return msg


class RequiredMeansRequired(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skirnir-protocol-")
        self.previous = state.CFG, state.REG, state.NODES, state.PENDING, state.spawn
        setup_router_state(self.tmp)
        state.CFG.mqtt = None
        state.spawn = lambda coro: coro.close() if hasattr(coro, "close") else None
        self.node = nodes.Node("synthetic-node", {"vram_total_gib": 24})
        self.node.state, self.node.hb_ts = "free", time.time()
        state.NODES = {self.node.name: self.node}

    def tearDown(self):
        state.CFG, state.REG, state.NODES, state.PENDING, state.spawn = self.previous
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hello(self, msg, nonce):
        return asyncio.run(registry.receive_hello(FakeWS(json.dumps(msg)), nonce))

    def test_hello_with_only_the_required_fields_is_accepted(self):
        nonce = os.urandom(32)
        msg = signed_hello(nonce, node="")
        self.assertEqual(strict("hello.schema.json", msg), [])
        got = self.hello(msg, nonce)
        self.assertFalse(registry.failed(got), got)

    def test_hello_without_a_required_field_is_refused(self):
        nonce = os.urandom(32)
        for field in SCHEMAS["hello.schema.json"]["required"]:
            msg = signed_hello(nonce)
            del msg[field]
            got = self.hello(msg, nonce)
            self.assertTrue(registry.failed(got), field)
            self.assertEqual(got.status, 401, field)

    def test_hello_signature_covers_the_node_name_and_its_limit(self):
        nonce = os.urandom(32)
        self.assertFalse(registry.failed(self.hello(signed_hello(nonce, node="a" * 64), nonce)))
        long_name = signed_hello(nonce, node="a" * 65)   # schema maxLength 64: the router cuts before verifying
        self.assertTrue(strict("hello.schema.json", long_name))
        self.assertTrue(registry.failed(self.hello(long_name, nonce)))
        self.assertTrue(registry.failed(self.hello(signed_hello(nonce, facts=["not", "an", "object"]), nonce)))

    def test_ollama_state_needs_up(self):
        poll.apply_ollama_push(self.node, {"tags": [], "ps": []}, time.time())
        self.assertEqual(self.node.ollama_push_ts, 0.0)
        now = time.time()
        poll.apply_ollama_push(self.node, {"up": True}, now)   # only the required field; null/absent lists count as empty
        self.assertEqual(self.node.ollama_push_ts, now)
        poll.apply_ollama_push(self.node, {"up": True, "tags": "x", "ps": []}, time.time())
        self.assertEqual(self.node.ollama_push_ts, 0.0, "up true with tags that are not a list must be dropped")
        for example in SCHEMAS["ollama-state.schema.json"]["examples"]:
            now = time.time()
            poll.apply_ollama_push(self.node, example, now)
            self.assertEqual(self.node.ollama_push_ts, now, example)

    def test_power_needs_a_known_state(self):
        poll.apply_power(self.node, {"via": "test"}, time.time())
        poll.apply_power(self.node, {"state": "hibernating"}, time.time())
        self.assertIsNone(self.node.power)
        for example in SCHEMAS["power.schema.json"]["examples"]:
            poll.apply_power(self.node, example, time.time())
            self.assertEqual(self.node.power, poll.POWER_STATES[example["state"]])

    def test_heartbeat_examples_are_accepted_and_answered(self):
        for example in SCHEMAS["heartbeat.schema.json"]["examples"]:
            poll.apply_heartbeat(self.node, example, time.time())
            self.assertEqual(strict("heartbeat-ack.schema.json", poll.hb_ack(self.node)), [])
        self.assertEqual(self.node.memory["ram_total_gib"], 63.7)
        self.assertEqual(self.node.routes, {"rerank": {"up": True, "reason": ""}})

    def test_heartbeat_answers_over_the_tunnel(self):
        sent = []
        tun = tunnel.Tunnel(None, None, "192.0.2.10")   # not approved yet
        tun.send_quiet = sent.append
        state.spawn = lambda frame: None
        tun.dispatch(tunnel.tun_frame(tunnel.TUN_HB, 0, b"{}"))
        tun.node, tun.approved = self.node, True
        tun.dispatch(tunnel.tun_frame(tunnel.TUN_HB, 0, json.dumps({"vram_total_mib": "not a number"}).encode()))
        tun.dispatch(tunnel.tun_frame(tunnel.TUN_HB, 0, json.dumps(SCHEMAS["heartbeat.schema.json"]["examples"][0]).encode()))
        acks = [json.loads(f[5:]) for f in sent]
        self.assertEqual([f[0] for f in sent], [tunnel.TUN_HBACK] * 3)
        self.assertEqual([a["state"] for a in acks], ["pending", "", "free"])
        self.assertIn("error", acks[1])
        for a in acks:
            self.assertEqual(strict("heartbeat-ack.schema.json", a), [], a)

    def test_configuration_bundle_conforms(self):
        e = {"name": "synthetic-node", "facts": SCHEMAS["facts.schema.json"]["examples"][0], "policy": {}}
        self.assertEqual(strict("config-bundle.schema.json", registry.provision_bundle(e)), [])
        state.CFG.mqtt = {"host": "broker.example.net", "port": 8883, "tls": True, "username": "skirnir", "password": "example-only"}
        bundle = registry.provision_bundle(e)
        self.assertIn("mqtt", bundle)
        self.assertEqual(strict("config-bundle.schema.json", bundle), [])

    def test_upstream_orders_conform(self):
        sent = []

        async def capture(tun, msg):
            sent.append(msg)

        self.node.upstream_releasable, self.node.tunnel = True, object()
        original, registry.send_ctl = registry.send_ctl, capture
        try:
            for what in ("hold", "release"):
                self.assertTrue(asyncio.run(openai_node.order(self.node, what, "selftest")))
        finally:
            registry.send_ctl = original
            self.node.tunnel = None
        self.assertEqual([m["t"] for m in sent], ["upstream-hold", "upstream-release"])
        for m in sent:
            self.assertEqual(strict("control-upstream.schema.json", m), [], m)


class Relay:
    """Sits between test/fake_agent.py and the real router; validates every message strictly and records it."""

    def __init__(self, router_client):
        self.router = router_client
        self.problems, self.seen = [], []
        self.models = [{"name": "example-model:8b", "size": 5000000000, "digest": "0" * 12}]

    def check(self, direction, msg):
        if msg.type == WSMsgType.TEXT:
            m = json.loads(msg.data)
            t = m.get("t")
            spec = INDEX["text_messages"].get(t)
            if spec is None or spec["direction"] != direction:
                self.problems.append(f"{direction}: undocumented text message {t!r}")
                return
            self.seen.append((direction, t))
            self.problems += [f"{direction} {t}: {p}" for p in strict(spec["schema"], m)]
            return
        data = msg.data
        if len(data) < 5 or data[0] not in BY_CODE:
            self.problems.append(f"{direction}: unknown binary frame {data[:1]!r}")
            return
        name, spec = BY_CODE[data[0]]
        sid, payload = int.from_bytes(data[1:5], "big"), data[5:]
        self.seen.append((direction, name))
        if spec["direction"] != direction:
            self.problems.append(f"{name} sent {direction}")
        if (spec["stream"] == "0") != (sid == 0):
            self.problems.append(f"{name} on stream {sid}")
        if name in FRAME_SCHEMA:
            self.problems += [f"{name}: {p}" for p in strict(FRAME_SCHEMA[name], json.loads(payload))]
        elif name == "REQ":
            head, nl, _ = payload.partition(b"\n")
            self.problems += ([] if nl else ["REQ without newline"]) + [f"REQ: {p}" for p in strict("request-head.schema.json", json.loads(head))]
        elif name in ("END", "CANCEL") and payload:
            self.problems.append(f"{name} with a payload")

    async def tunnel(self, request):
        agent = web.WebSocketResponse()
        await agent.prepare(request)
        router = await self.router.ws_connect("/v1/tunnel")

        async def pump(src, dst, direction):
            async for msg in src:
                if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                    break
                self.check(direction, msg)
                await (dst.send_str(msg.data) if msg.type == WSMsgType.TEXT else dst.send_bytes(msg.data))
            await dst.close()

        await asyncio.gather(pump(agent, router, "agent-to-router"), pump(router, agent, "router-to-agent"))
        return agent

    async def ollama(self, request):   # the fake agent's upstream
        return web.json_response({"models": self.models if request.path == "/api/tags" else []})


async def until(cond, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


class FakeAgentEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skirnir-protocol-e2e-")
        self.previous = state.CFG, state.REG, state.NODES, state.PENDING
        setup_router_state(self.tmp)
        _, ctl = app.build_apps()
        self.router = TestClient(TestServer(ctl, host="127.0.0.1"))
        await self.router.start_server()
        self.relay = Relay(self.router)
        relay_app = web.Application()
        relay_app.router.add_get("/v1/tunnel", self.relay.tunnel)
        relay_app.router.add_get("/api/tags", self.relay.ollama)
        relay_app.router.add_get("/api/ps", self.relay.ollama)
        self.relay_server = TestServer(relay_app, host="127.0.0.1")
        await self.relay_server.start_server()
        base = f"127.0.0.1:{self.relay_server.port}"
        env = {**os.environ, "FAKE_OLLAMA_MANAGED": "1", "FAKE_OLLAMA_PUSH": "1", "FAKE_OLLAMA_SWAP_GATE": "1"}
        self.agent = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_agent.py"), f"ws://{base}", "synthetic-node",
                                       f"http://{base}", os.path.join(self.tmp, "agent.key")],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    async def asyncTearDown(self):
        self.agent.kill()
        self.agent.wait(10)
        await self.relay_server.close()
        await self.router.close()
        state.CFG, state.REG, state.NODES, state.PENDING = self.previous
        registry._ENROLL.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_fake_agent_and_router_speak_the_specified_protocol(self):
        self.assertTrue(await until(lambda: len(state.PENDING) == 1), f"agent never registered: {self.relay.problems}")
        fp = next(iter(state.PENDING))
        key = base64.b64decode(state.REG.nodes[fp]["pubkey"])
        self.assertEqual(fp, hashlib.sha256(key).hexdigest(), "the identity is the SHA-256 of the public key")
        Ed25519PublicKey.from_public_bytes(key)   # a valid 32-byte Ed25519 key (raises otherwise)
        await registry.approve_node(fp, {})
        node = state.NODES["synthetic-node"]
        self.assertTrue(await until(lambda: node.hb_ts and node.ollama_push_ts), "no heartbeat or OLLAMA report applied")
        self.assertTrue(await until(lambda: "example-model:8b" in node.models), node.models)
        resp = await node.tunnel.open("GET", "/api/tags", b"", time.time() + 10)
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(await resp.read())["models"][0]["name"], "example-model:8b")
        self.assertTrue(await until(lambda: ("router-to-agent", "HBACK") in self.relay.seen))
        seen = set(self.relay.seen)
        for expected in [("router-to-agent", "challenge"), ("agent-to-router", "hello"), ("router-to-agent", "status"),
                         ("agent-to-router", "HB"), ("router-to-agent", "HBACK"), ("agent-to-router", "OLLAMA"),
                         ("router-to-agent", "REQ"), ("agent-to-router", "RESP"), ("agent-to-router", "DATA"), ("agent-to-router", "END")]:
            self.assertIn(expected, seen)
        self.assertEqual(self.relay.problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
