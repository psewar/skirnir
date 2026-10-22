#!/usr/bin/env python3
"""Self-test (Router 0.8.11): the rest of the router reports expected cases as a value, and the baseline is empty.

Decision engines return a Problem instead of EngineError, files (JSON, journal, model) are pre-checked, Basic Auth
and password hashes via pattern, the TLS fingerprint via pattern already when loading the configuration, and an aborted
Idempotency waiter no longer continues as the first request. Runs without router and network."""
import asyncio
import base64
import json
import os
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import auth, common, ops, state, toolcall_rescue  # noqa: E402
from skirnir_router.decision import Decider  # noqa: E402
from skirnir_router.decision.base import DecisionEngine, DecisionRequest, DecisionResult, engine_failed, normalize  # noqa: E402
from skirnir_router.decision.local_llm import chosen_option  # noqa: E402
from skirnir_router.decision.tfidf import TfidfEngine  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

sys.path.insert(0, HERE)
import check_control_flow  # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="skirnir-kf4-")

# --- Baseline: no legacy data left
with open(os.path.join(HERE, "control_flow_baseline.json"), encoding="utf-8") as f:
    check("Baseline empty: every exception in the router is marked or replaced", json.load(f) == {})
check("check_control_flow: no occurrence in the router", not any(check_control_flow.findings(p) for p in check_control_flow.SOURCES))

# --- Decision: Engines return Problem, the chain falls through
check("normalize: text instead of number -> empty", normalize({"a": "viel", "b": 1}, ["a", "b"]) == {})
check("normalize: no dict -> empty", normalize(["a"], ["a"]) == {})
check("normalize: valid", normalize({"a": 3, "b": 1}, ["a", "b"]) == {"a": 0.75, "b": 0.25})
check("local_llm: option from JSON", chosen_option('{"option": "code"}', ["code", "assist"]) == "code")
check("local_llm: JSON without object -> plain text search", chosen_option('["x"] assist', ["code", "assist"]) == "assist")
check("local_llm: kaputtes JSON -> Klartextsuche", chosen_option("{kaputt code", ["code", "assist"]) == "code")


class Outage(DecisionEngine):
    name = "ausfall"

    async def decide(self, req):
        return engine_failed("unreachable")


class Confident(DecisionEngine):
    name = "sicher"

    async def decide(self, req):
        return DecisionResult(selected="code", probabilities={"code": 0.95, "assist": 0.05}, engine=self.name, model=None, latency_ms=1.0)


state.CFG = SimpleNamespace(decision={"enabled": True, "options": ["code", "assist"]}, path=os.path.join(tmp, "config.yaml"))
decider = Decider({"options": ["code", "assist"], "chain": ["rules"]})
res = asyncio.run(decider.decide(DecisionRequest(context="x", options=["code", "assist"]), engines=[Outage(), Confident()]))
check("Chain: failed engine -> next decides", res.engine == "sicher" and res.fallback == ["ausfall: ausgefallen"], str(res.fallback))
check("DecisionEngine is abstract", getattr(DecisionEngine.decide, "__isabstractmethod__", False))

for what, content in (("fehlt", None), ("kaputt", "{"), ("unvollstaendig", json.dumps({"classes": ["a"]}))):
    path = os.path.join(tmp, f"tfidf-{what}.json")
    if content is not None:
        open(path, "w", encoding="utf-8").write(content)
    p = TfidfEngine.load(path)
    check(f"tfidf: model {what} -> Problem", failed(p), getattr(p, "message", p))

# --- Dateien
path = os.path.join(tmp, "x.json")
check("load_json_file: missing -> None", common.load_json_file(path, "x") is None)
open(path, "w", encoding="utf-8").write("{")
check("load_json_file: corrupt -> None", common.load_json_file(path, "x") is None)
check("save_json_file: schreibt atomar", common.save_json_file(path, {"a": 1}, "x") and common.load_json_file(path, "x") == {"a": 1})
check("save_json_file: fehlendes Verzeichnis -> False", common.save_json_file(os.path.join(tmp, "fehlt", "x.json"), {}, "x") is False)
check("save_json_file: not encodable -> False", common.save_json_file(path, {"a": {1, 2}}, "x") is False)
env = os.path.join(tmp, "secrets.env")
open(env, "w", encoding="utf-8").write('A=1\nB="zwei"\n')
check("read_env_value: key", common.read_env_value(env, "A") == "1")
check("read_env_value: quotes removed", common.read_env_value(env, "B", strip_quotes=True) == "zwei")
check("read_env_value: file missing -> None", common.read_env_value(os.path.join(tmp, "nix.env"), "A") is None)
check("Journal: broken line -> None", state._journal_row('{"event": "x", "t"') is None)
check("Journal: valid line", state._journal_row('{"event": "x", "t": 1}') == {"event": "x", "t": 1})

# --- Auth and TLS
cred = base64.b64encode(b"admin:geheim").decode()
check("basic_credentials: valid", common.basic_credentials(cred) == ("admin", "geheim"))
check("basic_credentials: no Base64 -> None", common.basic_credentials("@@@") is None)
check("basic_credentials: invalid UTF-8 is replaced", common.basic_credentials(base64.b64encode(b"a:\xff").decode()) == ("a", "\ufffd"))
stored = auth.hash_password("geheim", iterations=1000)
check("verify_password: correct", auth.verify_password("geheim", stored))
check("verify_password: incorrect", not auth.verify_password("falsch", stored))
check("verify_password: unlesbarer Eintrag -> False", not auth.verify_password("geheim", "pbkdf2:x:zz:yy"))
fp = "AB:" * 31 + "AB"
check("tls_fingerprint: with colons", common.tls_fingerprint("sha256:" + fp) == "ab" * 32)
check("tls_fingerprint: too short -> None", common.tls_fingerprint("abcd") is None)
check("tls_fingerprint: no hex -> None", common.tls_fingerprint("zz" * 32) is None)

# --- Tool call rescue only types what fits
check("rescue: integer", toolcall_rescue._fit_type(" 12 ", "integer") == 12)
check("rescue: integer with comma stays text", toolcall_rescue._fit_type("1,5", "integer") == "1,5")
check("rescue: number", toolcall_rescue._fit_type("1.5", "number") == 1.5)
check("rescue: infinite number stays text", toolcall_rescue._fit_type("inf", "number") == "inf")


# --- Idempotency: Aborting the waiter propagates upward
async def idem_cases():
    state.CFG = SimpleNamespace(request_timeout_s=0.2, idempotency_ttl_s=60)
    ops.IDEM.clear()
    first = await ops.idem_wait("k")
    check("idem: first request reserved", first is None and ops.IDEM["k"]["fut"] is not None)
    waiter = asyncio.ensure_future(ops.idem_wait("k"))
    await asyncio.sleep(0.05)
    waiter.cancel()
    cancelled = await asyncio.gather(waiter, return_exceptions=True)
    check("idem: aborted waiter ends as aborted (not as first request)",
          isinstance(cancelled[0], asyncio.CancelledError) and not ops.IDEM["k"]["fut"].done())
    timed_out = await ops.idem_wait("k")
    check("idem: Frist abgelaufen -> None", timed_out is None)

asyncio.run(idem_cases())

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
