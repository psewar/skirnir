#!/usr/bin/env python3
"""Self-test (TM-08, ADR-0135): a client's own monthly budget for cloud tiers.

clients.<c>.cloud_budget_month (catalogue currency; 0 or absent = no limit, as before) bounds what one client may spend
on cloud tiers in a calendar month, over all providers. The spend is the sum of the client's daily cost in usage.json
(metrics.usage_add, which every finished cloud request already feeds). Once it reaches the budget, the request's cloud
tiers are skipped with the reason (local tiers continue), like the provider's own budget. The configuration refuses
what is not a number in range; the client view shows budget and spend. Synthetic values only; no network or provider."""
import os
import sys
import time
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import auth, cloud, config, metrics, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402


def request():
    return SimpleNamespace(execution="auto", data_class="public", cloud_privacy=False, data_class_effective=None)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.previous = state.CFG, dict(state.USAGE)
        state.USAGE.clear()
        state.USAGE["days"] = {}
        state.CFG = SimpleNamespace(
            cloud={"enabled": True, "data_classes": ["public", "internal", "personal", "secret"],
                   "default_data_class": "personal", "max_cloud_data_class": "internal", "credential_scan": "block"},
            client_auth={"mode": "enforce", "clients": {"capped": {"cloud_budget_month": 1.5}, "open": {},
                                                        "zero": {"cloud_budget_month": 0}}})

    def tearDown(self):
        state.CFG = self.previous[0]
        state.USAGE.clear()
        state.USAGE.update(self.previous[1])

    def spend(self, client, cost, day=None):
        if day is None:
            metrics.usage_add(client, "assist", "openai:example-model", "ok", 10, 10, cost)
        else:   # an earlier day, as usage.json keeps it
            entry = state.USAGE["days"].setdefault(day, {"clients": {}})["clients"].setdefault(client, {})
            entry["cost_chf"] = entry.get("cost_chf", 0.0) + cost

    def test_spend_is_this_months_cost_of_this_client(self):
        this_month = time.strftime("%Y-%m")
        self.spend("capped", 0.4)
        self.spend("capped", 0.3, day=this_month + "-01")
        self.spend("capped", 5.0, day="2001-01-15")          # another month does not count
        self.spend("open", 2.0)                              # another client does not count
        self.assertEqual(cloud.client_spend_month("capped"), 0.7)
        self.assertEqual(cloud.client_spend_month("capped", "2001-01"), 5.0)
        self.assertEqual(cloud.client_spend_month("nobody"), 0.0)

    def test_an_exhausted_budget_skips_the_cloud_tiers(self):
        self.spend("capped", 1.0)
        self.assertIsNone(cloud.request_block_reason(request(), {"messages": []}, "capped"), "below the budget")
        self.spend("capped", 0.5)
        reason = cloud.request_block_reason(request(), {"messages": []}, "capped")
        self.assertEqual(reason, "client capped: monthly cloud budget 1.50 exhausted", "at the budget: cloud skipped")
        self.spend("open", 1000.0)
        self.spend("zero", 1000.0)
        self.assertIsNone(cloud.request_block_reason(request(), {"messages": []}, "open"), "no budget: no limit")
        self.assertIsNone(cloud.request_block_reason(request(), {"messages": []}, "zero"), "0: no limit, as documented")
        self.assertIsNone(cloud.request_block_reason(request(), {"messages": []}, None), "unidentified: unchanged")

    def test_the_client_view_shows_budget_and_spend(self):
        self.spend("capped", 0.25)
        view = auth.client_auth_view()["clients"]
        self.assertEqual((view["capped"]["cloud_budget_month"], view["capped"]["cloud_spend_month"]), (1.5, 0.25))
        self.assertEqual((view["open"]["cloud_budget_month"], view["open"]["cloud_spend_month"]), (0, 0.0))

    def test_configuration_takes_only_numbers_in_range(self):
        def parse(value):
            return config.Config._parse_client_auth(SimpleNamespace(), {"mode": "enforce", "clients": {
                "c": {"token_sha256": "0" * 64, "models": False, "cloud_budget_month": value}}})
        for value in (0, 5, 12.5, 100_000):
            with self.subTest(value=value):
                self.assertIsNone(parse(value))
        for value in (-1, 100_001, "5", True, [5]):
            with self.subTest(value=value):
                problem = parse(value)
                self.assertTrue(failed(problem), f"{value!r} is refused")
                self.assertIn("cloud_budget_month", problem.message)


if __name__ == "__main__":
    unittest.main(verbosity=1)
