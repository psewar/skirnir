#!/usr/bin/env python3
"""Self-test (router 0.16.4): switching to the standby waits for a gap on the OpenAI-compatible server.

Trigger 2026-10-24: a memory write's request for an Ollama model held the server while an overnight agent's request
was streaming from it; the hold stopped the server at once and broke the stream off after 5.8 s. Checks that the wait
sees only requests on the server (not other requests waiting to switch, which would wait for each other), that
ensure_standby sends no hold while the server answers and sends it once the gap comes, and the server_busy verdict.
Runs without network, agent or server."""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, openai_node, scheduler, state  # noqa: E402

FAILS = []
BIG, DENSE = "big-moe", "dense:27b"


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class FakeTunnel:
    pass


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
ORDERS = []


async def fake_order(node, what, reason):
    ORDERS.append((what, time.time()))
    node.upstream_order = what
    return True


openai_node.order = fake_order


def server_node():
    """The server answers BIG; the stopped standby has DENSE."""
    n = nodes.Node("strata-box", {"vram_total_gib": 31.8, "upstream_api": "openai", "upstream_releasable": True,
                                  "upstream_fallback": True})
    n.tunnel = FakeTunnel()
    n.state, n.hb_ts = "free", time.time()
    n.models, n.loaded = {BIG}, {BIG: 0.0}
    n.digest_of = {BIG: "openai:" + BIG}
    n.standby_models = {DENSE}
    return n


def run_request(n, model):
    n.inflight += 1
    n.inflight_models[model] += 1


def end_request(n, model):
    n.inflight -= 1
    n.inflight_models[model] -= 1


async def main():
    n = server_node()
    check("setup: DENSE is reachable only by switching", n.standby_switch(DENSE) and not n.standby_switch(BIG))

    # 1) What counts: requests on the server, not requests waiting to switch
    run_request(n, BIG)
    check("one answer on the server -> server_inflight 1", n.server_inflight() == 1, str(n.server_inflight()))
    run_request(n, DENSE)
    run_request(n, DENSE)
    check("two switch requests besides it do not count", n.server_inflight() == 1, str(n.server_inflight()))
    end_request(n, BIG)
    check("server idle, two switch requests waiting -> gap (they do not wait for each other)", n.server_inflight() == 0)
    end_request(n, DENSE)
    end_request(n, DENSE)

    # 2) wait_for_server_gap: deadline while busy, True once the answer ends
    run_request(n, BIG)
    t = time.time()
    ok = await openai_node.wait_for_server_gap(n, time.time() + 0.6)
    check("server busy until the deadline -> False, after the deadline", not ok and time.time() - t >= 0.6)

    async def finish_later(delay):
        await asyncio.sleep(delay)
        end_request(n, BIG)
    asyncio.get_running_loop().create_task(finish_later(0.7))
    t = time.time()
    ok = await openai_node.wait_for_server_gap(n, time.time() + 10)
    check("answer ends after 0.7 s -> True, not before", ok and time.time() - t >= 0.6, f"{time.time() - t:.2f} s")
    check("no gap needed when idle -> True at once", await openai_node.wait_for_server_gap(n, time.time()))

    # 3) ensure_standby sends no hold while the server answers
    n = server_node()
    run_request(n, BIG)
    run_request(n, DENSE)   # the switching request itself (proxy counts it before ensure_standby)
    ORDERS.clear()
    ok = await openai_node.ensure_standby(n, DENSE, timeout_s=0.6)
    check("ensure_standby while the server answers until its timeout -> False, no hold", not ok and ORDERS == [], str(ORDERS))

    n = server_node()
    run_request(n, BIG)
    run_request(n, DENSE)
    ORDERS.clear()
    t_end = []

    async def answer_ends_then_standby_serves():
        await asyncio.sleep(0.7)
        end_request(n, BIG)
        t_end.append(time.time())
        await asyncio.sleep(0.6)   # the agent reports the standby serving with DENSE
        n.upstream_serving, n.models = openai_node.SERVING_OLLAMA, {BIG, DENSE}
    asyncio.get_running_loop().create_task(answer_ends_then_standby_serves())
    ok = await openai_node.ensure_standby(n, DENSE, timeout_s=10)
    check("ensure_standby: hold only after the running answer ended, then True",
          ok and [o for o, _ in ORDERS] == ["hold"] and ORDERS[0][1] >= t_end[0], str(ORDERS))

    # 4) The verdict when the wait runs out
    n = server_node()
    run_request(n, BIG)
    why = scheduler.server_busy(n, {"model": DENSE, "num_ctx": 196608, "busy_ok": False}, 42.4)
    check("server_busy: code and Retry-After", why["code"] == scheduler.SERVER_BUSY and why["retry_after_s"] == 30, str(why))
    check("server_busy: detail names node, running answers and wait",
          "strata-box answers 1 request(s)" in why["detail"] and "waited 42 s" in why["detail"], why["detail"])


asyncio.run(main())
print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
