#!/usr/bin/env python3
"""Repeatable synthetic projection benchmark. No real model/Hindsight quality claim."""
import copy
import hashlib
import json
import statistics
import time
from dataclasses import replace

from selftest_context import ContextConfig, encoded, estimate, history, project, row


def benchmark():
    context = history()
    context["messages"].insert(1, row(1, "developer", "SYNTHETIC_DEVELOPER_GUARD"))
    context["messages"][3] = row(3, "assistant", "", tool_calls=[{"id": "call-benchmark", "function": {"name": "example", "arguments": {}}}])
    context["messages"][4] = row(4, "tool", "Synthetic completed result " * 2000, tool_call_id="call-benchmark", tool_name="example")
    context["checkpoint"] = {"version": 1, "payload": {"schema_version": 1, "goals": ["SYNTHETIC_GOAL"], "open_steps": ["SYNTHETIC_PENDING_STEP"]}}
    for entry in context["messages"]:
        if entry["seq"] in (18, 19):
            entry["payload"]["content"] = "Synthetic recent conversation. " * 8
    original = copy.deepcopy(context)
    cfg = ContextConfig(tail_messages=2, summary_bytes=256, reduce_tool_results=True, tool_result_bytes=256)
    results = []
    baseline = estimate({}, [row["payload"] for row in context["messages"]])
    for budget in (4096, 8192, 16384):
        timings, hashes = [], set()
        for _ in range(20):
            started = time.perf_counter()
            messages, version = project(context, {}, budget, cfg, "synthetic-model")
            timings.append((time.perf_counter() - started) * 1000)
            hashes.add(hashlib.sha256(encoded([messages, version])).hexdigest())
            assert estimate({}, messages) <= budget
            for guard in ("SYNTHETIC_DEVELOPER_GUARD", "SYNTHETIC_GOAL", "SYNTHETIC_PENDING_STEP", "Current question"):
                assert any(guard in m.get("content", "") for m in messages)
            assert any(m.get("tool_call_id") == "call-benchmark" for m in messages)
            assert any(m.get("tool_calls") and m["tool_calls"][0]["id"] == "call-benchmark" for m in messages)
            assert context == original
        assert len(hashes) == 1
        results.append({"prompt_budget": budget, "estimated_tokens": estimate({}, messages), "baseline_estimated_tokens": baseline,
                        "reduction_ratio": round(1 - estimate({}, messages) / baseline, 4),
                        "protected_markers_retained": 4, "tool_pair_consistent": True, "unique_projections_over_20_runs": len(hashes),
                        "projection_median_ms": round(statistics.median(timings), 3)})
    # Switching window sizes must not erase system/checkpoint/raw input. The final
    # projection remains byte-for-byte reproducible, independent of timing data.
    _, first = project(context, {}, 16384, cfg, "large-synthetic-model")
    switched = {**context, "compaction": {**first, "version": 1},
                "messages": [r for r in context["messages"] if r["seq"] > first["through_seq"]]}
    smaller, _ = project(switched, {}, 4096, replace(cfg, summary_bytes=128), "small-synthetic-model")
    assert estimate({}, smaller) <= 4096
    return {"fixture": "synthetic-v1", "real_service_tested": False, "results": results, "model_switch_fits": True}


if __name__ == "__main__":
    print(json.dumps(benchmark(), indent=2))
