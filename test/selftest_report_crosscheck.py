#!/usr/bin/env python3
"""Self-test (TM-07, ADR-0142): an approved agent's reports are checked against each other and against what the router
observes.

poll.report_contradictions finds (1) free_vram: more free VRAM than the reported card can have next to the models the
same agent reports loaded (only when they fit that card, so a second GPU does not count), and (2) gpu_idle: GPU
utilisation at about zero while the router is receiving generated output from the node (its own observation, set by the
relay). poll.note_report_contradictions makes a contradiction a fact only after MISMATCH_HEARTBEATS consecutive
heartbeats: on the node (view), as a problem (UI, Home Assistant, metric) and as one audit line; it clears when the reports
agree again. Routing is unchanged. Synthetic values only; no agent or node."""
import inspect
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import auth, config, nodes, poll, problems, proxy, state  # noqa: E402

HB = {"gpu_util_pct": 40, "vram_total_mib": 24576, "vram_used_mib": 16384, "vram_free_mib": 8192}


class CrossCheckTests(unittest.TestCase):
    def setUp(self):
        self.previous = state.CFG, dict(state.NODES)
        state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
        self.node = nodes.Node("synthetic-node", {"vram_total_gib": 24.0})
        self.node.loaded = {"synthetic-model:14b": 14.0}
        self.node.state = "free"   # online, as a node with a running agent is (problems.compute looks at online nodes)
        state.NODES.clear()
        state.NODES[self.node.name] = self.node
        self.audited = []
        self.real_audit = auth.audit
        auth.audit = lambda event, **fields: self.audited.append((event, fields))

    def tearDown(self):
        auth.audit = self.real_audit
        state.CFG = self.previous[0]
        state.NODES.clear()
        state.NODES.update(self.previous[1])

    def beat(self, now, **values):
        poll.apply_heartbeat(self.node, {**HB, **values}, now)

    def test_free_vram_beyond_the_loaded_models(self):
        now = time.time()
        self.beat(now, vram_free_mib=20 * 1024)   # 20 GiB free on a 24 GiB card with 14 GiB loaded: at most 10 fit
        self.assertEqual(poll.report_contradictions(self.node, now)["free_vram"], {"free": 20.0, "loaded": 14.0, "room": 10.0})
        self.beat(now, vram_free_mib=int(10.5 * 1024))
        self.assertNotIn("free_vram", poll.report_contradictions(self.node, now), "within the slack")
        self.node.loaded = {"a:1": 20.0, "b:1": 10.0}   # more than the card: a second GPU, not a lie
        self.beat(now, vram_free_mib=20 * 1024)
        self.assertNotIn("free_vram", poll.report_contradictions(self.node, now))
        self.node.loaded = {}
        self.assertNotIn("free_vram", poll.report_contradictions(self.node, now), "nothing loaded: nothing to compare")

    def test_gpu_idle_while_the_router_receives_output(self):
        now = time.time()
        self.node.last_output_at = now - 1
        self.beat(now, gpu_util_pct=0)
        self.assertEqual(poll.report_contradictions(self.node, now)["gpu_idle"], {"util": 0})
        self.beat(now, gpu_util_pct=35)
        self.assertNotIn("gpu_idle", poll.report_contradictions(self.node, now), "busy GPU while generating: consistent")
        self.node.last_output_at = now - 120
        self.beat(now, gpu_util_pct=0)
        self.assertNotIn("gpu_idle", poll.report_contradictions(self.node, now), "no recent output: an idle GPU is fine")
        self.node.last_output_at, self.node.gpu_present = now - 1, False
        self.assertNotIn("gpu_idle", poll.report_contradictions(self.node, now), "a node without GPU has no utilisation")

    def test_a_contradiction_counts_only_when_it_persists_and_clears_again(self):
        now = time.time()
        state_before = self.node.state
        for i in range(poll.MISMATCH_HEARTBEATS - 1):
            self.beat(now + i, vram_free_mib=20 * 1024)
        self.assertEqual(self.node.report_mismatch, {}, "a single or short glitch is not reported")
        with self.assertLogs("router", level="WARNING") as logs:
            self.beat(now + 5, vram_free_mib=20 * 1024)
        self.assertIn("free_vram", self.node.report_mismatch)
        self.assertIn("agent report contradicts observation (free_vram)", logs.output[0])
        self.assertEqual([e for e, _ in self.audited], ["agent_report_mismatch"])
        self.assertEqual(self.audited[0][1]["node"], "synthetic-node")
        self.beat(now + 6, vram_free_mib=20 * 1024)
        self.assertEqual(len(self.audited), 1, "one audit line per contradiction, not per heartbeat")
        codes = [p["code"] for p in problems.compute(now + 6)]
        self.assertIn("agent_free_vram_mismatch", codes, "a problem for UI, Home Assistant and metrics")
        self.assertIn("free_vram", self.node.snapshot(now + 6)["report_mismatch"], "node view")
        self.assertEqual(self.node.state, state_before, "routing is not changed")
        self.beat(now + 7, vram_free_mib=8 * 1024)
        self.assertEqual(self.node.report_mismatch, {}, "agreeing reports clear it")
        self.assertNotIn("agent_free_vram_mismatch", [p["code"] for p in problems.compute(now + 7)])

    def test_both_kinds_raise_their_own_problem(self):
        now = time.time()
        self.node.last_output_at = now
        for i in range(poll.MISMATCH_HEARTBEATS):
            self.beat(now + i, gpu_util_pct=0, vram_free_mib=20 * 1024)
        messages = {p["code"]: p["message"] for p in problems.compute(now + 3, lang="en")}
        self.assertEqual(messages["agent_idle_while_generating"],
                         "node synthetic-node: reports 0 % GPU load while it generates output for the router (agent report implausible)")
        self.assertIn("only 10.0 GiB fit next to 14.0 GiB of loaded models", messages["agent_free_vram_mismatch"])

    def test_the_relay_records_its_own_observation(self):
        # instrument: the native stream loop stamps the node when a line of output arrives (not from the agent's report)
        source = inspect.getsource(proxy._Relay)
        self.assertIn("self.node.last_output_at = time.time()", source)


if __name__ == "__main__":
    unittest.main(verbosity=1)
