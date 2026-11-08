"""Self-test (TM-07): agent enrollment, revocation and enrollment flood over the real tunnel endpoint.

Runs the router's control app in-process and talks to /v1/tunnel with a real WebSocket client that signs the challenge
with an Ed25519 key, like the agent. Checks:
  * a new key waits as pending; approval attaches it and tells the agent;
  * revoking ends a request that is waiting for the agent's answer and one whose body is streaming, tells the agent,
    closes the connection and removes the node from routing;
  * the revoked key reconnecting is refused and never becomes pending or a node again;
  * new keys from one address beyond the enrollment limit are refused at the wire (1008), an approved key still connects.
No network, no agent binary."""
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

from aiohttp import ClientError, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import app, config, registry, state, tunnel  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")


class Agent:
    """Minimal agent side of the tunnel handshake and stream protocol."""

    def __init__(self, client, name):
        self.client, self.name = client, name
        self.key = Ed25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.fp = __import__("hashlib").sha256(self.pub).hexdigest()
        self.ws = None

    async def connect(self):
        self.ws = await self.client.ws_connect("/v1/tunnel")
        challenge = await self.ws.receive_json(timeout=5)
        nonce = base64.b64decode(challenge["nonce"])
        sig = self.key.sign(nonce + self.name.encode() + self.pub)
        await self.ws.send_json({"t": "hello", "node": self.name, "pubkey": base64.b64encode(self.pub).decode(),
                                 "sig": base64.b64encode(sig).decode(), "facts": {"hostname": self.name}})
        return await self.next_json()

    async def next_json(self):
        while True:
            msg = await self.ws.receive(timeout=5)
            if msg.type == WSMsgType.TEXT:
                return json.loads(msg.data)
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                return {"closed": True, "code": self.ws.close_code}

    async def next_binary(self):
        while True:
            msg = await self.ws.receive(timeout=5)
            if msg.type == WSMsgType.BINARY:
                return msg.data
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                raise AssertionError("connection closed while waiting for a frame")

    async def until_closed(self):
        seen = []
        while True:
            msg = await self.ws.receive(timeout=5)
            if msg.type == WSMsgType.TEXT:
                seen.append(json.loads(msg.data))
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                return seen


class RevocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skirnir-revocation-")
        self.previous = state.CFG, state.REG, state.NODES, state.PENDING
        state.CFG = config.Config(EXAMPLE, use_overrides=False)
        state.REG = registry.NodeRegistry(os.path.join(self.tmp, "nodes.json")).load()
        state.NODES, state.PENDING = {}, {}
        registry._ENROLL.clear()
        _, ctl = app.build_apps()
        self.client = TestClient(TestServer(ctl, host="127.0.0.1"))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        state.CFG, state.REG, state.NODES, state.PENDING = self.previous
        registry._ENROLL.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def approved_agent(self, name):
        agent = Agent(self.client, name)
        status = await agent.connect()
        self.assertEqual(status.get("state"), "pending", status)
        self.assertIn(agent.fp, state.PENDING)
        await registry.approve_node(agent.fp, {})
        approved = await agent.next_json()
        self.assertEqual(approved.get("state"), "approved", approved)
        self.assertIsNotNone(state.NODES[name].tunnel)
        return agent

    async def test_revocation_ends_active_streams_and_bars_the_key(self):
        agent = await self.approved_agent("synthetic-gpu")
        node = state.NODES["synthetic-gpu"]
        # Stream 1 waits for the agent's response head; stream 2 already has a head and is streaming its body.
        waiting = asyncio.create_task(node.tunnel.open("POST", "/api/chat", b"{}", time.time() + 10))
        first = await agent.next_binary()
        self.assertEqual(first[0], tunnel.TUN_REQ)
        streaming_task = asyncio.create_task(node.tunnel.open("POST", "/api/generate", b"{}", time.time() + 10))
        second = await agent.next_binary()
        sid = int.from_bytes(second[1:5], "big")
        await agent.ws.send_bytes(tunnel.tun_frame(tunnel.TUN_RESP, sid, json.dumps({"status": 200, "headers": {}}).encode()))
        await agent.ws.send_bytes(tunnel.tun_frame(tunnel.TUN_DATA, sid, b'{"response":"partial"}\n'))
        streaming = await asyncio.wait_for(streaming_task, 5)
        self.assertEqual(streaming.status, 200)

        await registry.revoke_node(agent.fp)

        with self.assertRaises(ClientError) as waited:
            await asyncio.wait_for(waiting, 5)
        self.assertIn("revoked", str(waited.exception))
        with self.assertRaises(ClientError) as body:
            await asyncio.wait_for(streaming.read(), 5)
        self.assertIn("revoked", str(body.exception))
        told = await agent.until_closed()
        self.assertTrue(any(m.get("state") == "revoked" for m in told), told)
        self.assertNotIn("synthetic-gpu", state.NODES)
        self.assertEqual(state.REG.nodes[agent.fp]["state"], "revoked")

        # The same key comes back: refused before it can wait or route again.
        again = await agent.connect()
        self.assertEqual(again.get("state"), "revoked", again)
        self.assertEqual((await agent.until_closed()), [])
        self.assertNotIn(agent.fp, state.PENDING)
        self.assertNotIn("synthetic-gpu", state.NODES)

    async def test_enrollment_flood_is_refused_at_the_wire(self):
        known = await self.approved_agent("synthetic-known")
        await known.ws.close()
        registry._ENROLL.clear()
        accepted = []
        for i in range(registry.MAX_ENROLL_PER_IP):
            agent = Agent(self.client, f"synthetic-new-{i}")
            status = await agent.connect()
            self.assertEqual(status.get("state"), "pending", status)
            accepted.append(agent)
        flood = Agent(self.client, "synthetic-flood")
        refused = await flood.connect()
        self.assertEqual(refused.get("state"), "pending", refused)   # the agent is told to wait, with the refusal text
        closed = await flood.next_json()
        self.assertTrue(closed.get("closed") and closed.get("code") == 1008, closed)
        self.assertNotIn(flood.fp, state.REG.nodes, "a refused key is not written to the registry")
        self.assertNotIn(flood.fp, state.PENDING)
        # An approved key still gets in while the address is over its limit for new keys.
        status = await known.connect()
        self.assertEqual(status.get("state"), "approved", status)
        for agent in accepted:
            await agent.ws.close()
        await known.ws.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
