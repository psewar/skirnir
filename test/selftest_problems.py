#!/usr/bin/env python3
"""Self-test (Router 0.35.0, ADR-0101): problems as structured records.

Until router 0.34 the only alert channel was a list of German sentences in the Home Assistant state; without MQTT an
operator learned nothing. Problems now come from problems.py as records with a fixed code and severity. Checked here:
1. the catalogue (codes, severities, a German and an English text for every code),
2. every code from its real condition, with the German text byte for byte as before (HA automations match it),
3. the HA view keeps its shape and German texts by default; router.problem_language en switches the texts only,
4. /metrics: skirnir_problem{code,severity,node,subject} and skirnir_problems_active, equal label sets summed, a line
   feed in a reported name escaped,
5. GET /admin/problems through the real control app: 401 without credentials, 400 for an unknown language, records with
   credentials,
6. configuration: default de, an unknown language is refused.
Runs without router, network and Ollama (real config from router/config.example.yaml)."""
import asyncio
import base64
import copy
import os
import re
import sys
import time
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import agentupdate, app, auth, cloud, config, ha, metrics, nodes, ollamaupdate, problems, state   # noqa: E402
from skirnir_router.outcome import failed   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
state.CFG = config.Config(EXAMPLE, use_overrides=False)

# --- 1. Catalogue
check("every code is snake_case English", all(re.fullmatch(r"[a-z][a-z0-9_]*", c) for c in problems.CODES), str(problems.CODES))
check("every severity is warning or critical", all(sev in problems.SEVERITIES for sev, _ in problems.CATALOGUE.values()))
check("every code has a German and an English text",
      all(set(texts) == set(problems.LANGUAGES) for _, texts in problems.CATALOGUE.values()))
check("critical = requests fail or a tier drops out now",
      {c for c, (s, _) in problems.CATALOGUE.items() if s == "critical"} == {"no_node_online", "role_not_servable", "cloud_budget_exhausted"})


# --- 2. Every code from its condition
def node(name, **kw):
    n = nodes.Node(name, {"vram_total_gib": 24.0})
    n.state = "free"
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def scenario(now):
    """One state that raises every code once (except no_node_online, which needs no node online)."""
    silent = node("silent-node", hb_ts=now - 3600)                       # agent reported, then silent for an hour
    spill = node("spill-node", hb_ts=now, ram_spill={"synth-model:27b": 3.2})
    guard = node("guard-node", hb_ts=now, guard={"state": "unavailable", "problem": True, "reason": "limit not settable"},
                 guard_ts=now)
    lying = node("lying-node", hb_ts=now, report_mismatch={"free_vram": {"since": now, "free": 20.0, "loaded": 14.0, "room": 10.0},
                                                           "gpu_idle": {"since": now, "util": 0}})   # ADR-0142
    state.NODES = {n.name: n for n in (silent, spill, guard, lying)}
    state.REG = SimpleNamespace(nodes={
        "a" * 64: {"name": "upd-failed", "state": "approved", "update": {"state": "failed", "message": "checksum mismatch"}},
        "b" * 64: {"name": "upd-stalled", "state": "approved", "update": {"state": "stalled", "message": "no report for 31 min"}},
        "c" * 64: {"name": "upd-none", "state": "approved", "update": {}},
    }, save=lambda: None)
    state.CLOUD = {name: provider(name, spec) for name, spec in (("synth-cloud", {"budget_month_chf": 10.0, "warn_at_percent": 80}),
                                                                 ("synth-spent", {"budget_month_chf": 5.0}), ("synth-free", {}))}


def provider(name, spec):
    """Stand-in for a CloudTarget: the spec, a snapshot for the HA view, a key for /metrics."""
    snap = {"enabled": True, "key": True, "spend_month_chf": SPEND[name], "budget_month_chf": spec.get("budget_month_chf", 0),
            "budget_percent": 0, "breaker": "closed", "inflight": 0}
    return SimpleNamespace(spec=spec, api_key="synthetic", snapshot=lambda: snap)


SPEND = {"synth-cloud": 8.5, "synth-spent": 5.25, "synth-free": 3.0}
cloud.spend_month = lambda name: SPEND.get(name, 0.0)
agentupdate.update_view = lambda e, now=None: dict(e.get("update") or {})
ollamaupdate.update_view = lambda e, now=None: dict(e.get("update") or {})
state.CFG.ollama_update = {"enabled": True}
ollamaupdate.LATEST.clear()
ollamaupdate.LATEST.update({"error": "HTTP 503", "version": None})
state.CFG.gpu_guard = {**state.CFG.gpu_guard, "enabled": True}

now = time.time()
scenario(now)
recs = problems.compute(now)
by_code = {}
for r in recs:
    by_code.setdefault(r["code"], []).append(r)
expected_codes = set(problems.CODES) - {"no_node_online"}
check("every code except no_node_online raised by the scenario", set(by_code) == expected_codes,
      str(sorted(set(by_code) ^ expected_codes)))

GERMAN = {   # the sentences of router 0.34, byte for byte
    ("agent_heartbeat_missing", "silent-node"): "Knoten silent-node: kein Agent-Heartbeat seit 60 min",
    ("ram_spill", "spill-node"): "Knoten spill-node: synth-model:27b liegt zu 3.2 GiB im Arbeitsspeicher (rechnet langsam auf der CPU)",
    ("gpu_guard", "guard-node"): "Knoten guard-node: GPU-Schutz unavailable (limit not settable)",
    ("agent_free_vram_mismatch", "lying-node"): "Knoten lying-node: meldet 20.0 GiB freien VRAM, neben 14.0 GiB geladenen Modellen passen nur 10.0 GiB (Agent-Meldung unplausibel)",
    ("agent_idle_while_generating", "lying-node"): "Knoten lying-node: meldet 0 % GPU-Last, waehrend er Ausgabe fuer den Router erzeugt (Agent-Meldung unplausibel)",
    ("agent_update_failed", "upd-failed"): "Agent-Update auf upd-failed fehlgeschlagen: checksum mismatch",
    ("agent_update_stalled", "upd-stalled"): "Agent-Update auf upd-stalled haengt (no report for 31 min)",
    ("ollama_update_failed", "upd-failed"): "Ollama-Update auf upd-failed fehlgeschlagen: checksum mismatch",
    ("ollama_update_stalled", "upd-stalled"): "Ollama-Update auf upd-stalled haengt (no report for 31 min)",
    ("ollama_version_check_failing", None): "Ollama-Versionspruefung scheitert: HTTP 503",
    ("cloud_budget_warning", None): "Cloud synth-cloud: 85 % des Monatsbudgets (8.50/10.00)",
    ("cloud_budget_exhausted", None): "Cloud synth-spent: Monatsbudget erschoepft (5.25/5.00)",
}
for (code, nd), text in GERMAN.items():
    got = [r["message"] for r in by_code.get(code, []) if r["node"] == nd]
    check(f"{code}: German text as before", got == [text], str(got))
roles_down = [r for r in by_code.get("role_not_servable", [])]
check("role_not_servable: one per role, subject = role, German text as before",
      roles_down and all(r["subject"] == r["params"]["role"] and r["message"] == f"Rolle {r['subject']} nicht bedienbar"
                         and r["node"] is None for r in roles_down), str(roles_down[:2]))
check("ram_spill: node and model as identities", by_code["ram_spill"][0]["node"] == "spill-node"
      and by_code["ram_spill"][0]["subject"] == "synth-model:27b")
check("cloud budget: provider as subject, no node",
      [(r["subject"], r["node"]) for r in by_code["cloud_budget_warning"] + by_code["cloud_budget_exhausted"]]
      == [("synth-cloud", None), ("synth-spent", None)])
check("severity follows the catalogue", all(r["severity"] == problems.CATALOGUE[r["code"]][0] for r in recs))
check("records have exactly the six fields", all(set(r) == {"code", "severity", "node", "subject", "params", "message"} for r in recs))

state.CFG.ram_spill_problem_gib = 0
check("ram_spill_problem_gib 0 -> no ram_spill problem", not any(r["code"] == "ram_spill" for r in problems.compute(now)))
state.CFG.ram_spill_problem_gib = 0.5
state.CFG.gpu_guard = {**state.CFG.gpu_guard, "enabled": False}
check("gpu_guard disabled in the router -> no gpu_guard problem", not any(r["code"] == "gpu_guard" for r in problems.compute(now)))
state.CFG.gpu_guard = {**state.CFG.gpu_guard, "enabled": True}

for n in state.NODES.values():
    n.state = "offline"
off = problems.compute(now)
check("no node online -> no_node_online first, German text as before",
      off and off[0]["code"] == "no_node_online" and off[0]["message"] == "kein Knoten online" and off[0]["severity"] == "critical",
      str(off[:1]))
for n in state.NODES.values():
    n.state = "free"

en = {r["code"]: r["message"] for r in problems.compute(now, lang="en")}
check("English texts", en["ram_spill"] == "node spill-node: synth-model:27b has 3.2 GiB in system RAM (runs slowly on the CPU)"
      and en["cloud_budget_exhausted"] == "cloud synth-spent: monthly budget exhausted (5.25/5.00)"
      and en["agent_heartbeat_missing"] == "node silent-node: no agent heartbeat for 60 min", str(en))
inj = problems.make("ram_spill", node="n", subject="{node}", model="{node.__class__}", gib=1.0)
check("a reported name is substituted once, never expanded", problems.message(inj, "en").startswith("node n: {node.__class__} has"))

# --- 3. HA view: same shape, German by default, English on request
snap = ha.ha_snapshot(now)
check("HA problems = German messages in the same order", snap["problems"] == [r["message"] for r in problems.compute(now)]
      and snap["problem"] is True and all(isinstance(p, str) for p in snap["problems"]))
check("HA keeps roles_limited and the role view", "roles_limited" in snap and isinstance(snap["roles"], dict))
state.CFG.problem_language = "en"
snap_en = ha.ha_snapshot(now)
check("problem_language en -> HA texts English, same count",
      len(snap_en["problems"]) == len(snap["problems"]) and "node spill-node: synth-model:27b has 3.2 GiB in system RAM (runs slowly on the CPU)" in snap_en["problems"])
state.CFG.problem_language = "de"

# --- 4. /metrics
text = metrics.render()
lines = text.splitlines()
check("skirnir_problems_active = number of problems", f"skirnir_problems_active {len(recs)}" in lines,
      [x for x in lines if x.startswith("skirnir_problems_active")])
check("skirnir_problem for ram_spill with node and subject",
      'skirnir_problem{code="ram_spill",severity="warning",node="spill-node",subject="synth-model:27b"} 1' in lines)
check("skirnir_problem for a problem without node: empty node label",
      'skirnir_problem{code="ollama_version_check_failing",severity="warning",node="",subject=""} 1' in lines)
check("no free text in labels (update message stays out)", "checksum mismatch" not in text and "HTTP 503" not in text)
rows = problems.metric_rows([problems.make("ram_spill", node="n", subject="m", model="m", gib=1.0)] * 2)
check("equal label sets are summed, no duplicate series", rows == [({"code": "ram_spill", "severity": "warning", "node": "n", "subject": "m"}, 2)], str(rows))
check("line feed in a label value is escaped", metrics._labels({"subject": "a\nb"}) == '{subject="a\\nb"}', metrics._labels({"subject": "a\nb"}))
codes_in_metrics = set(re.findall(r'^skirnir_problem\{code="([a-z_]+)"', text, re.M))
check("every metric code is a catalogue code", codes_in_metrics <= set(problems.CODES) and codes_in_metrics == expected_codes,
      str(codes_in_metrics ^ expected_codes))


# --- 5. GET /admin/problems through the real control app
async def endpoint():
    users = state.CFG.control_users
    state.CFG.control_users = {"tester": auth.hash_password("synthetic-pass", iterations=1000)}
    _, ctl = app.build_apps()
    c = TestClient(TestServer(ctl, host="127.0.0.1"))
    await c.start_server()
    basic = {"Authorization": "Basic " + base64.b64encode(b"tester:synthetic-pass").decode()}
    wrong = {"Authorization": "Basic " + base64.b64encode(b"tester:wrong-pass").decode()}
    got = {}
    got["anon"] = (await c.get("/admin/problems")).status
    got["wrong"] = (await c.get("/admin/problems", headers=wrong)).status
    r = await c.get("/admin/problems?lang=xx", headers=basic)
    got["bad_lang"] = r.status
    r = await c.get("/admin/problems", headers=basic)
    got["ok"], got["body"] = r.status, await r.json()
    r = await c.get("/admin/problems?lang=en", headers=basic)
    got["en"] = await r.json()
    await c.close()
    state.CFG.control_users = users
    return got


g = asyncio.run(endpoint())
check("GET /admin/problems without credentials -> 401", g["anon"] == 401, str(g["anon"]))
check("GET /admin/problems with a wrong password -> 401", g["wrong"] == 401, str(g["wrong"]))
check("GET /admin/problems?lang=xx -> 400", g["bad_lang"] == 400, str(g["bad_lang"]))
body = g["body"]
check("GET /admin/problems -> 200 with records in the configured language",
      g["ok"] == 200 and body["language"] == "de" and body["count"] == len(recs) and len(body["problems"]) == len(recs)
      and body["by_severity"]["critical"] + body["by_severity"]["warning"] == len(recs), str({k: body[k] for k in ("count", "language", "by_severity")}))
check("... records with code, severity, node, subject, params, message",
      all({"code", "severity", "node", "subject", "params", "message"} <= set(p) for p in body["problems"]))
check("?lang=en -> English texts, same codes", g["en"]["language"] == "en"
      and [p["code"] for p in g["en"]["problems"]] == [p["code"] for p in body["problems"]]
      and any(p["message"].startswith("node spill-node:") for p in g["en"]["problems"]))

# --- 6. Configuration
check("config default: problem_language de", config.Config(EXAMPLE, use_overrides=False).problem_language == "de")
raw = config.load_yaml(EXAMPLE)
bad = copy.deepcopy(raw)
bad["router"]["problem_language"] = "fr"
cfg_bad = config.Config.__new__(config.Config)
res = cfg_bad._parse_router(bad["router"])
check("problem_language fr -> refused as a value, not an exception", failed(res) and "problem_language" in res.message, str(res))
check("problem_language is UI-editable as a choice of the catalogue languages",
      config.settings_mod.EDITABLE["router.problem_language"]["choices"] == list(problems.LANGUAGES)
      and config.settings_mod.EDITABLE["router.problem_language"]["default"] == "de")

print()
print(f"{len(FAILS)} failures" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
