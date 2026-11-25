#!/usr/bin/env python3
"""Self-test (TM-07, ADR-0137): the router uses an agent's GPU numbers only when they are plausible.

poll.plausible_gpu_facts keeps each GPU number of a heartbeat only within the heartbeat schema's range (gpu_util_pct
0-100; VRAM amounts non-negative, finite, below 4 TiB) and free VRAM and the Ollama process only when not above the
total (plus a small slack); used VRAM above the total only makes the node look busier and is kept. A dropped value is unknown (None), as for a node without GPU; poll.apply_heartbeat applies
only the kept values, lists the dropped fields on the node (shown in the node view) and logs them at most every ten
minutes. A malformed number no longer raises half-way through the heartbeat. Synthetic values only; no agent."""
import json
import math
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, poll, state  # noqa: E402

SCHEMA = os.path.join(HERE, "..", "docs", "protocol", "heartbeat.schema.json")
GOOD = {"gpu_util_pct": 4, "vram_total_mib": 24564, "vram_used_mib": 9100, "vram_free_mib": 15464,
        "ollama_proc_mib": 7000, "children_vram_mib": {"stt": 1800}}


class FactTests(unittest.TestCase):
    def test_the_schema_examples_pass_unchanged(self):
        with open(SCHEMA, encoding="utf-8") as f:
            examples = json.load(f)["examples"]
        self.assertGreater(len(examples), 1, "instrument: the schema has examples")
        for example in examples:
            facts, dropped = poll.plausible_gpu_facts(example)
            self.assertEqual(dropped, [], example)
            for key in ("gpu_util_pct", "vram_total_mib", "vram_used_mib", "vram_free_mib"):
                self.assertEqual(facts[key], example.get(key), key)

    def test_values_out_of_range_are_dropped(self):
        for key, value in (("gpu_util_pct", 150), ("gpu_util_pct", -1), ("gpu_util_pct", "50"), ("gpu_util_pct", True),
                           ("gpu_util_pct", math.nan), ("vram_total_mib", -5), ("vram_total_mib", 1 << 30),
                           ("vram_total_mib", "24564"), ("vram_free_mib", math.inf), ("ollama_proc_mib", [1])):
            with self.subTest(key=key, value=value):
                facts, dropped = poll.plausible_gpu_facts({**GOOD, key: value})
                self.assertIsNone(facts[key])
                self.assertIn(key, dropped)

    def test_room_above_the_total_is_dropped(self):
        for key in ("vram_free_mib", "ollama_proc_mib"):
            with self.subTest(key=key):
                facts, dropped = poll.plausible_gpu_facts({**GOOD, key: GOOD["vram_total_mib"] + 4096})
                self.assertEqual((facts[key], dropped), (None, [key]))
        facts, dropped = poll.plausible_gpu_facts({**GOOD, "vram_free_mib": GOOD["vram_total_mib"] + 100})
        self.assertEqual(dropped, [], "rounding slack above the total is accepted")
        facts, dropped = poll.plausible_gpu_facts({**GOOD, "vram_used_mib": GOOD["vram_total_mib"] + 12000})
        self.assertEqual((facts["vram_used_mib"], dropped), (GOOD["vram_total_mib"] + 12000, []),
                         "used above the total only makes the node look busier; kept (end-to-end foreign load)")

    def test_children_keep_only_plausible_amounts(self):
        facts, dropped = poll.plausible_gpu_facts({**GOOD, "children_vram_mib": {"stt": 1800, "x": -1, "y": "big"}})
        self.assertEqual((facts["children_vram_mib"], dropped), ({"stt": 1800}, ["children_vram_mib"]))
        self.assertIsNone(poll.plausible_gpu_facts({})[0]["children_vram_mib"])


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.previous = state.CFG
        state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
        self.node = nodes.Node("synthetic-node", {"vram_total_gib": 24.0})

    def tearDown(self):
        state.CFG = self.previous

    def test_a_malformed_heartbeat_is_applied_without_its_bad_numbers(self):
        now = time.time()
        with self.assertLogs("router", level="WARNING") as logs:
            poll.apply_heartbeat(self.node, {**GOOD, "gpu_util_pct": "busy", "vram_used_mib": "9100"}, now)
        self.assertEqual((self.node.gpu_util, self.node.vram_used_gib), (None, None), "dropped values are unknown")
        self.assertAlmostEqual(self.node.vram_free_gib, GOOD["vram_free_mib"] / 1024)
        self.assertEqual(self.node.implausible_fields, ["gpu_util_pct", "vram_used_mib"])
        self.assertIn("heartbeat values out of range ignored: gpu_util_pct, vram_used_mib", logs.output[0])
        self.assertEqual(self.node.snapshot(now)["implausible_fields"], ["gpu_util_pct", "vram_used_mib"], "node view")
        # the next bad heartbeat within the interval is not logged again; a good one clears the list
        with self.assertNoLogs("router", level="WARNING"):
            poll.apply_heartbeat(self.node, {**GOOD, "gpu_util_pct": 500}, now + 5)
        poll.apply_heartbeat(self.node, dict(GOOD), now + 10)
        self.assertEqual((self.node.implausible_fields, self.node.gpu_util), ([], 4))

    def test_a_lying_load_report_is_not_used(self):
        # a node claiming more free VRAM than its card has is not offered more room than the card has
        poll.apply_heartbeat(self.node, {**GOOD, "vram_free_mib": 10 * GOOD["vram_total_mib"]}, time.time())
        self.assertIsNone(self.node.vram_free_gib)
        self.assertEqual(self.node.implausible_fields, ["vram_free_mib"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
