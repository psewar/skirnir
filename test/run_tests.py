#!/usr/bin/env python3
"""End-to-end tests against the router with two fake Ollama nodes. Start: python run_tests.py"""
import json
import os
import subprocess
import threading
import sys
import yaml
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
import ssl
R = "https://127.0.0.1:21434"
C = "https://127.0.0.1:21435"
SSL_NOVERIFY = ssl.create_default_context()
SSL_NOVERIFY.check_hostname = False
SSL_NOVERIFY.verify_mode = ssl.CERT_NONE
FAILS = []


import base64
import hashlib
AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:geheim").decode()}


LAST_HEADERS = {}   # Response headers of the last http() request (tier 1: X-Skirnir-*)


def http(url, body=None, headers=None, method=None, timeout=30, auth=True):
    global LAST_HEADERS
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    if auth and url.startswith(C) and "/v1/" not in url:
        hdrs.update(AUTH)
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_NOVERIFY if url.startswith("https") else None) as r:
            raw = r.read()
            LAST_HEADERS = dict(r.headers)
            return r.status, raw
    except urllib.error.HTTPError as e:
        LAST_HEADERS = dict(e.headers)
        return e.code, e.read()


REQ_T = 0.0   # Timestamp of the last request, for route()


class Decision(dict):
    """dict that returns None for missing keys: if route() found nothing, the check should fail cleanly
    instead of aborting the whole run with a KeyError."""

    def __missing__(self, key):
        return None


def chat(model, ctx=None, stream=True, **extra):
    global REQ_T
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream, **extra}
    if ctx:
        body["options"] = {"num_ctx": ctx}
    REQ_T = time.time()
    st, raw = http(R + "/api/chat", body)
    return st, raw.decode()


def route(since=None):
    """The routing decision for the last request (or since `since`).

    Do NOT use decisions[-1]: free-, busy-, prewarm- or unload-events may land in between, unrelated to
    your own request - this is exactly why the 'warm first' test failed sporadically.
    """
    t0 = REQ_T if since is None else since
    ds = [d for d in state()["decisions"] if d["event"] == "route" and d.get("t", 0) >= t0]
    return Decision(ds[-1] if ds else {})


def hb(node, util, used, free, total=32607, **extra):
    return http(C + f"/v1/heartbeat/{node}", {"gpu_util_pct": util, "vram_total_mib": total, "vram_used_mib": used,
                                             "vram_free_mib": free, **extra}, headers={"X-Router-Token": "testtoken"})


def state():
    return json.loads(http(C + "/admin/state")[1])


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def last_route(node_proc_out, n=1):
    lines = [json.loads(l) for l in node_proc_out.splitlines() if l.strip().startswith("{")]
    return lines[-n:]


def main():
    procs = []
    logs = {}
    # self-signed certificate for the TLS control port (openssl from Git for Windows / System)
    if not os.path.exists(os.path.join(HERE, "test-cert.pem")):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-keyout", "test-key.pem", "-out", "test-cert.pem", "-days", "30", "-subj", "/CN=localhost"],
                       cwd=HERE, check=True, capture_output=True)
    for f in ("roles.yaml", "perf.json", "nodes.json", "fake-agent-big.key", "audit.jsonl", "usage.json", "decisions.jsonl", "events.jsonl", "metrics.json", "agent-update.pub", "agent/manifest.json", "ollama-latest.json", "fake-agent-tray.key"):   # usage.json: otherwise cloud costs of the previous run are counted   # remove leftovers from earlier run
        try:
            os.remove(os.path.join(HERE, f))
        except FileNotFoundError:
            pass
    for port, name, models in ((21001, "big", "qwen3.6:35b-a3b,qwen3-coder:30b,granite4.2:8b,local-assist:latest,gpt-oss:20b"), (21002, "small", "granite4.2:8b")):
        logs[name] = open(os.path.join(HERE, f"fake-{name}.log"), "w+")
        extra = ["--tls", "test-cert.pem", "test-key.pem", "--require-token", "testtoken"] if name == "big" else []
        procs.append(subprocess.Popen([PY, os.path.join(HERE, "fake_ollama.py"), str(port), name, models, "--delay", "0.05", *extra], cwd=HERE,
                                      stdout=logs[name], stderr=subprocess.STDOUT))
    logs["jevlike"] = open(os.path.join(HERE, "fake-jevlike.log"), "w+")   # Decision Engine: Fake-Jevlike
    procs.append(subprocess.Popen([PY, os.path.join(HERE, "fake_jevlike.py"), "21020"], cwd=HERE, stdout=logs["jevlike"], stderr=subprocess.STDOUT))
    logs["cloud"] = open(os.path.join(HERE, "fake-cloud.log"), "w+")   # Tier 5: OpenAI and Anthropic fakes on one port
    procs.append(subprocess.Popen([PY, os.path.join(HERE, "fake_cloud.py"), "21010", "cloud-test-key", "claude-test-key"], cwd=HERE,
                                  stdout=logs["cloud"], stderr=subprocess.STDOUT))
    open(os.path.join(HERE, "secrets.env"), "w", encoding="utf-8").write("CLOUD_TEST_KEY=cloud-test-key\nCLAUDE_TEST_KEY=claude-test-key\n")
    rlog = open(os.path.join(HERE, "router.log"), "w+")
    router = subprocess.Popen([PY, os.path.join(HERE, "..", "router", "router.py"), os.path.join(HERE, "test-config.yaml")],
                              stdout=rlog, stderr=subprocess.STDOUT, cwd=HERE)
    procs.append(router)
    # big comes via tunnel: fake agent connects to the router and forwards to the fake Ollama (TLS + token)
    logs["agent-big"] = open(os.path.join(HERE, "fake-agent-big.log"), "w+")
    agent = subprocess.Popen([PY, os.path.join(HERE, "fake_agent.py"), "wss://127.0.0.1:21435", "big", "https://127.0.0.1:21001", os.path.join(HERE, "fake-agent-big.key"), "testtoken"],
                             stdout=logs["agent-big"], stderr=subprocess.STDOUT, cwd=HERE)
    procs.append(agent)
    try:
        # Registration: router does not know big -> key lands as "awaiting approval" in the register
        reg = None
        for _ in range(30):
            time.sleep(0.5)
            try:
                reg = json.loads(http(C + "/admin/nodes")[1])
                if any(n["name"] == "big" for n in reg["nodes"]): break
            except Exception: pass  # noqa: BLE001
        big = next((n for n in (reg or {"nodes": []})["nodes"] if n["name"] == "big"), None)
        check("Agent-Schluessel registriert -> wartet auf Freigabe", big is not None and big["state"] == "pending" and big["connected"], str(big and {k: big[k] for k in ("state", "connected")}))
        check("Fakten vom Agenten uebernommen (GPU, VRAM, MAC)", big and big["facts"].get("gpu") == "Fake RTX 5090" and big["facts"].get("vram_total_mib") == 32563 and big["facts"].get("mac") == "00:11:22:33:44:55", str(big and big["facts"]))
        check("pending-Knoten wird NICHT geroutet", "big" not in state()["nodes"], str(list(state()["nodes"])))
        ha = json.loads(http(C + "/admin/ha")[1])
        check("/admin/ha zaehlt wartende Knoten", ha["nodes_pending"] == 1 and ha["pending"] == ["big"], str(ha.get("pending")))
        logs["agent-big"].seek(0); check("Agent hat Status 'pending' erhalten", '"status": "pending"' in logs["agent-big"].read())
        st, raw = http(C + f"/admin/nodes/{big['fp']}/approve", {"wol": True, "weight": 3, "mqtt": True, "foreign_vram_baseline_gib": 7.0})
        check("Freigabe mit Policy (WOL, Gewicht 3, Baseline 7)", st == 200, raw.decode()[:100])
        for _ in range(20):
            if state()["nodes"].get("big", {}).get("state") == "free": break
            time.sleep(0.5)
        s = state()
        check("beide Knoten online", all(n["state"] == "free" for n in s["nodes"].values()) and "big" in s["nodes"], str({k: v["state"] for k, v in s["nodes"].items()}))
        b = s["nodes"]["big"]
        check("big aus Fakten+Policy: 31.8 GiB, WOL, Gewicht 3", abs(b.get("vram_total_gib", 0) - 31.8) < 0.1 and b["wol"] is True and b["weight"] == 3 and b["fingerprint"] == big["fp"][:16], str({k: b.get(k) for k in ("vram_total_gib", "wol", "weight", "fingerprint")}))
        regbig = next(n for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")
        check("Register merkt sich die Modelle des Knotens (WOL nach Router-Neustart)", "qwen3-coder:30b" in (regbig.get("models") or []), str(regbig.get("models")))
        time.sleep(1)
        logs["agent-big"].seek(0); alog = logs["agent-big"].read()
        check("Agent hat Freigabe + Konfigurationspaket erhalten", '"status": "approved"' in alog and '"heartbeat_interval_s"' in alog, alog[-200:])
        check("Heartbeat durch den Tunnel beantwortet", '"hback"' in alog, alog[-160:])
        for _ in range(20):
            if state()["nodes"]["big"].get("ollama_source") == "push": break
            time.sleep(0.5)
        check("Ollama-Zustand von big kommt per Push vom Agenten, der Router pollt big nicht (Agent 0.14.0)",
              state()["nodes"]["big"].get("ollama_source") == "push" and "qwen3-coder:30b" in (regbig.get("models") or []), str(state()["nodes"]["big"].get("ollama_source")))

        st, raw = http(R + "/api/tags")
        names = [m["name"] for m in json.loads(raw)["models"]]
        roles = [n for n in names if n.split(":")[0] in ("assist", "code", "gross", "standard")]
        check("/api/tags zeigt Rollen + konkrete Modelle", len(roles) == 4 and "qwen3-coder:30b" in names and "granite4.2:8b" in names, str(names))

        st, raw = http(R + "/api/show", {"model": "standard"})
        check("/api/show für Rolle ohne Tag", st == 200 and "tools" in raw.decode(), f"{st}")

        time.sleep(5)   # prewarm waits up to 4 s for a heartbeat (none present -> node without agent)
        pre = [d for d in state()["decisions"] if d["event"] == "prewarm"]
        check("prewarm beim Online-Gehen: qwen auf big vorgeladen", any(p["node"] == "big" and p["model"] == "qwen3.6:35b-a3b" and p["ctx"] == 65536 for p in pre), str(pre))
        st, txt = chat("standard:latest", ctx=65536)
        lines = [json.loads(l) for l in txt.strip().splitlines()]
        check("stream: Rollenname im Chunk", st == 200 and all(l["model"] == "standard:latest" for l in lines), f"{st} {len(lines)} chunks")
        d = route()
        check("standard -> tier0 auf big, ctx 65536, warm (vorgeladen)", d["node"] == "big" and d["tier"] == 0 and d["ctx"] == 65536 and d["warm"] is True, str(d))
        logs["big"].seek(0); sent = last_route(logs["big"].read())[-1]
        check("keep_alive -1 gesetzt (free)", sent["keep_alive"] == -1 and sent["num_ctx"] == 65536, str(sent))

        st, txt = chat("standard", ctx=8192, stream=False)
        j = json.loads(txt)
        check("non-stream + Name ohne Tag; ctx nach unten begrenzt", st == 200 and j["model"] == "standard:latest", txt[:100])
        d = route()
        # since router 0.3.7: qwen is loaded with 65536 (<= tier) -> the role gets the loaded context instead of a reload
        # to 8192 (qwen3.8 does not share the runner across different num_ctx; proxy._backend_body, selftest_context_sharing.py)
        check("Rolle mit kleinerem num_ctx nimmt den geladenen Kontext (65536 <= Stufe, kein Reload)", d["ctx"] == 65536, str(d["ctx"]))

        time.sleep(1.5)  # Poll holt /api/ps -> qwen geladen
        hb("big", 2, 26600, 6000)   # 5.9 GiB free: qwen (21.4) fits only because loaded Ollama VRAM (20.6) counts as evictable
        st, txt = chat("local-assist")
        d = route()
        check("Alias (gleicher Digest) als konkretes Modell -> warm auf big", st == 200 and d["node"] == "big" and d["warm"] is True, str(d))
        logs["big"].seek(0); sent = last_route(logs["big"].read())[-1]
        # qwen is still loaded with 65536 (the 8192 request above used it) -> alias request sends 65536, not the Ollama default
        check("Alias ohne num_ctx uebernimmt geladenen Kontext (65536, kein Reload)", sent["num_ctx"] == 65536, str(sent))
        # Foreign VRAM transients (model switch): a single outlier must not make it busy
        hb("big", 5, 30000, 2600); time.sleep(0.6); hb("big", 5, 26600, 6000)
        check("einzelner Fremd-VRAM-Ausreisser bleibt free", state()["nodes"]["big"]["state"] == "free", state()["nodes"]["big"]["busy_reason"])
        # GPU guard (agent >= 0.7.0): status in heartbeat -> cap, HA problem, event; no effect on free/busy.
        # The first three messages come as from an agent before 0.20.0 (German), the fourth with the names from 0.20.0.
        base_cap = state()["nodes"]["big"]["max_inflight"]
        st, txt = hb("big", 5, 26600, 6000, gpu_guard={"state": "gedrosselt", "limit_w": 403, "target_w": 403, "default_limit_w": 575, "problem": False, "quelle": "nvml"})
        n = state()["nodes"]["big"]
        check("GPU-Schutz gedrosselt (Agent < 0.20) -> throttled, Deckel 1 gleichzeitig, free bleibt", n["max_inflight"] == 1 and n["gpu_guard"]["state"] == "throttled" and n["state"] == "free", str(n.get("gpu_guard")))
        check("Heartbeat-Antwort traegt gpu_guard_ack", st == 200 and json.loads(txt).get("gpu_guard_ack") is True, txt[:120])
        hb("big", 5, 26600, 6000, gpu_guard={"state": "unverfuegbar", "problem": True, "grund": "Limit nicht setzbar: keine Berechtigung (rc 4)"})
        ha_s = json.loads(http(C + "/admin/ha")[1])
        check("GPU-Schutz unverfuegbar -> HA-Problem, Deckel wieder normal", any("GPU-Schutz unavailable" in p for p in ha_s["problems"]) and state()["nodes"]["big"]["max_inflight"] == base_cap, str(ha_s["problems"]))
        hb("big", 5, 26600, 6000, gpu_guard={"state": "aus", "problem": True, "grund": "abgewaehlt"})
        ha_s = json.loads(http(C + "/admin/ha")[1])
        check("GPU-Schutz abgewaehlt -> HA-Problem (Betreiber-Entscheid)", any("deselected" in p for p in ha_s["problems"]), str(ha_s["problems"]))
        hb("big", 5, 26600, 6000, gpu_guard={"state": "normal", "limit_w": 460, "target_w": 460, "problem": False, "source": "nvml+gpuz"})
        ha_s = json.loads(http(C + "/admin/ha")[1])
        check("GPU-Schutz normal -> kein Problem, Deckel normal, HA-Knoten traegt Limit", not any("GPU-Schutz" in p for p in ha_s["problems"]) and state()["nodes"]["big"]["max_inflight"] == base_cap and ha_s["nodes"]["big"]["gpu_guard"]["limit_w"] == 460, str(ha_s["nodes"]["big"].get("gpu_guard")))
        evs = [d for d in state()["decisions"] if d.get("event") == "gpu_guard"]
        check("gpu_guard-Ereignisse im Entscheidungsprotokoll (je Zustandswechsel)", len(evs) >= 4 and evs[-1]["state"] == "normal", str([e["state"] for e in evs]))
        m_txt = http(C + "/metrics")[1]
        m_txt = m_txt.decode() if isinstance(m_txt, bytes) else str(m_txt)
        check("Metrik skirnir_node_gpu_guard", 'skirnir_node_gpu_guard{node="big",state="normal"} 1' in m_txt, "")
        # Decision log persistent: events.jsonl next to the config, one line per entry; a fresh process loads it
        decs = state()["decisions"]
        ev_lines = [json.loads(l) for l in open(os.path.join(HERE, "events.jsonl"), encoding="utf-8") if l.strip()]
        check("events.jsonl: jeder Eintrag des Protokolls steht als Zeile in der Datei", len(ev_lines) >= len(decs) and ev_lines[-1]["ts"] == decs[-1]["ts"] and ev_lines[-1]["event"] == decs[-1]["event"], f"{len(ev_lines)} Zeilen, {len(decs)} im Speicher")
        prog = "import sys; sys.path.insert(0, sys.argv[1]); from skirnir_router import state; C = type('C', (), {'path': sys.argv[2]}); " "state.CFG = C(); n = state.decisions_load(); print(n, state.DECISIONS[-1]['event'], len(state.DECISIONS))"
        rc_l = subprocess.run([PY, "-c", prog,
                               os.path.join(HERE, "..", "router"), os.path.join(HERE, "test-config.yaml")], capture_output=True, text=True, cwd=HERE)
        check("events.jsonl: frischer Prozess laedt die Eintraege (Neustart)", rc_l.returncode == 0 and rc_l.stdout.split()[:2] == [str(len(ev_lines)), decs[-1]["event"]], (rc_l.stdout + rc_l.stderr)[-160:])
        st, txt = chat("assist:latest")
        d = route()
        check("assist: warmes Modell (tier0 geladen) gewinnt", d["tier"] == 0 and d["warm"] is True, str(d))
        # 0.6.6: interactive falls back if the warm model is at its max_parallel limit; normal keeps waiting
        qwen_kat = {"weights_gib": 20.6, "kv_gib_per_1k": 0.001}
        st_q, raw_q = http(C + "/admin/config", {"models": {"qwen3.6:35b-a3b": {**qwen_kat, "max_parallel": 1}}}, method="PUT")
        lang = {}
        th_l = threading.Thread(target=lambda: lang.update(r=http(R + "/api/chat", {"model": "standard:latest", "stream": False,
                                                                                    "messages": [{"role": "user", "content": "lang [[sleep_s=3]]"}]})))
        th_l.start()
        time.sleep(0.6)
        t_i = time.time()
        # num_ctx 4096: granite fits on small (8 GB; @16384 does not) - otherwise only big remains, and the fallback model on big
        # would shift the VRAM calculation of the busy tests further down (as happens on the first run)
        st_i, raw_i = http(R + "/api/chat", {"model": "assist:latest", "stream": False, "options": {"num_ctx": 4096},
                                             "messages": [{"role": "user", "content": "Licht an"}]})
        dt_i = time.time() - t_i
        d_i = route(t_i)
        check("interactive bei belegtem warmem qwen (max_parallel 1) -> sofort granite auf dem ANDEREN Knoten (ausweichen), kein Warten",
              st_q == 200 and st_i == 200 and d_i.get("model") == "granite4.2:8b" and d_i.get("reason") == "fallback" and dt_i < 2.0
              and d_i.get("node") == "small",
              f"{st_q} {st_i} {dt_i:.2f}s {d_i}")
        t_n = time.time()
        st_n, raw_n = http(R + "/api/chat", {"model": "standard:latest", "stream": False, "messages": [{"role": "user", "content": "normal"}]})
        d_n = route(t_n)
        q_n = [d for d in state()["decisions"] if d.get("event") == "queued" and d.get("t", 0) >= t_n]
        check("... normal wartet weiter auf das warme qwen (queued max_parallel, dann qwen warm)", st_n == 200
              and d_n.get("model") == "qwen3.6:35b-a3b" and d_n.get("warm") is True and q_n and q_n[0].get("reason") == "max_parallel",
              f"{st_n} {d_n} {q_n}")
        th_l.join(15)
        http(C + "/admin/config", {"models": {"qwen3.6:35b-a3b": qwen_kat}}, method="PUT")

        st, txt = chat("code:latest", ctx=65536)
        d = route()
        check("code 65536: 17.3+6.3+0.8=24.4 passt in 31.8-1 -> tier0", d["tier"] == 0 and d["model"] == "qwen3-coder:30b", str(d))

        # big becomes busy: 60 % utilization, 2 s sustain
        # First: only utilization, no foreign VRAM (= other Ollama client) -> must NOT become busy
        for _ in range(4):
            hb("big", 65, 31300, 1300); time.sleep(0.7)    # 30.6 GiB belegt = 23.6 coder@65k (17.3+0.096*65.5) + 7 Baseline -> foreign 0
        check("Auslastung ohne fremdes VRAM bleibt free (anderer Ollama-Client)", state()["nodes"]["big"]["state"] == "free")
        # Then: game -> utilization + 1.7 GiB foreign VRAM
        for _ in range(4):
            hb("big", 65, 33400, 0); time.sleep(0.7)       # 32.6 = 23.6 + 7 + 2.0 fremd
        s = state()
        check("big -> busy durch gpu_util (+fremdes VRAM)", s["nodes"]["big"]["state"] == "busy",
              json.dumps({k: s["nodes"]["big"].get(k) for k in ("state", "busy_reason", "gpu_util", "vram_used_gib", "vram_free_gib", "foreign_vram_gib", "baseline_gib", "loaded", "inflight", "gpu_known", "heartbeat_age_s", "breaker")}))
        time.sleep(0.5)
        for _ in range(2):
            hb("big", 65, 9000, 23600); time.sleep(0.5)    # qwen unloaded: 8.8 GiB = 7 baseline + 1.8 game
        logs["big"].seek(0); out = logs["big"].read()
        check("unload_on_busy entlädt das grosse Modell (nicht granite)", '"unload": "qwen3-coder:30b"' in out and '"unload": "granite4.2:8b"' not in out)

        st, txt = chat("standard:latest", ctx=65536)
        d = route()
        check("standard bei busy big -> granite tier1 (busy_ok)", st == 200 and d["model"] == "granite4.2:8b" and d["tier"] == 1, str(d))
        # big busy: 20.1-2 = 18.1 GiB budget -> granite@32768 (4.95+5.2+0.8=10.9) fits on big; small (8-1=7) does not
        check("... und zwar auf big (small zu klein für 32k)", d["node"] == "big", d["node"])
        st, txt = chat("gross:latest")
        check("gross bei busy big ohne Fallback -> 503", st == 503, f"{st} {txt[:80]}")
        # Router 0.3.4: the client gets the reason as code + Retry-After, not just "no node available"
        e503 = json.loads(txt) if st == 503 else {}
        check("... mit Grund-Code gpu_busy, Retry-After und Hindernis je Knoten", e503.get("code") == "gpu_busy"
              and LAST_HEADERS.get("Retry-After") == "30" and any(b.get("node") == "big" for b in e503.get("blockers", [])),
              f"{e503.get('code')} {LAST_HEADERS.get('Retry-After')} {str(e503.get('blockers'))[:120]}")
        st, raw = http(R + "/v1/chat/completions", {"model": "gross:latest", "messages": [{"role": "user", "content": "hi"}]})
        oe = json.loads(raw).get("error", {}) if st == 503 else {}
        check("... OpenAI-Form: error.code gpu_busy, error.retry_after 30", oe.get("code") == "gpu_busy" and oe.get("retry_after") == 30,
              f"{st} {raw[:160]!r}")
        st, raw = http(R + "/v1/messages", {"model": "gross:latest", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
                       headers={"anthropic-version": "2023-06-01"})
        ae = json.loads(raw) if raw else {}
        check("... Anthropic-Form: HTTP 529 overloaded_error, Retry-After 30, error.skirnir.code gpu_busy", st == 529
              and ae.get("type") == "error" and ae.get("error", {}).get("type") == "overloaded_error"
              and LAST_HEADERS.get("Retry-After") == "30" and ae["error"].get("skirnir", {}).get("code") == "gpu_busy", f"{st} {raw[:200]!r}")
        st, raw = http(R + "/v1/skirnir/availability/gross:latest")
        av = json.loads(raw) if st == 200 else {}
        check("Verfuegbarkeit gross bei busy big: available false, code gpu_busy", av.get("available") is False and av.get("code") == "gpu_busy",
              f"{st} {raw[:160]!r}")
        st, raw = http(R + "/v1/skirnir/availability/standard:latest")
        check("Verfuegbarkeit standard (busy_ok-Stufe): available true", st == 200 and json.loads(raw).get("available") is True, f"{st} {raw[:160]!r}")
        ha = json.loads(http(C + "/admin/ha")[1])
        check("/admin/ha bei busy: gross ist 'limited_by_busy', KEIN Problem", ha["roles"]["gross"]["ready"] is False
              and ha["roles"]["gross"].get("limited_by_busy") is True and "gross" in ha["roles_limited"]
              and not any("gross" in p for p in ha["problems"]), json.dumps({"problems": ha["problems"], "limited": ha["roles_limited"]}))

        # Safety net: large model lands in VRAM despite busy (e.g. load race) -> router unloads it afterwards.
        # Heartbeats keep running throughout (otherwise the node becomes briefly free due to heartbeat staleness).
        for _ in range(3):
            hb("big", 65, 33400, 0); time.sleep(0.7)
        def unloads():
            logs["big"].seek(0); return logs["big"].read().count('"unload": "qwen3-coder:30b"')
        before = unloads()
        http("https://127.0.0.1:21001/api/generate", {"model": "qwen3-coder:30b", "keep_alive": -1, "options": {"num_ctx": 8192}}, headers={"X-Router-Token": "testtoken"}, auth=False)
        after = before
        # up to ~7 s: minimum interval of the safety net (unload_on_busy_interval_s, 2 s in test) + poll (1 s) +
        # asynchronous unload; used > total, so foreign VRAM remains despite extra model
        for _ in range(10):
            hb("big", 65, 45000, 0); time.sleep(0.7)
            after = unloads()
            if after > before:
                break
        check("busy-Sicherheitsnetz: nachtraeglich geladenes grosses Modell wird wieder entladen", after > before and state()["nodes"]["big"]["state"] == "busy",
              f"unloads {before} -> {after}, state {state()['nodes']['big']['state']}")
        # 0.6.8 hysteresis: a game with load between exit (20 %) and entry threshold (40 %) and little foreign VRAM
        # (1.8 GiB: below or_foreign_vram_gib 3.0, above util_requires_foreign_gib 1.0) keeps busy longer than below_for_s (2 s)
        # Starting point is the router's own calculation (busy - foreign = Ollama + children + baseline); the sum of
        # loaded models did not match (first run: foreign 0.0, Ollama process counts more than model sizes)
        nb = state()["nodes"]["big"]
        used_spiel = int((nb["vram_used_gib"] - nb["foreign_vram_gib"] + 1.8) * 1024)
        for _ in range(6):
            hb("big", 25, used_spiel, 32607 - used_spiel); time.sleep(0.7)
        nb = state()["nodes"]["big"]
        check("Hysterese: Spiel mit 25 % Last und ~1,8 GiB fremdem VRAM haelt busy (free erst unter 20 %), kein Flattern",
              nb["state"] == "busy", f"{nb['state']} util {nb['gpu_util']} fremd {nb['foreign_vram_gib']} geladen {nb.get('loaded')}")
        # busy over: heartbeats until the node is free, then continue IMMEDIATELY (the next check must be before the
        # prewarm-on-free, which starts 6 s after the switch)
        for _ in range(12):
            hb("big", 3, 8000, 24600); time.sleep(0.7)
            if state()["nodes"]["big"]["state"] == "free":
                break
        check("big -> free nach Ruhe", state()["nodes"]["big"]["state"] == "free")

        # big is free again, granite (tier 2) still warm, qwen cold: warm first -> granite; strict -> qwen cold
        st, txt = chat("standard:latest", ctx=16384)
        d = route()
        check("warm zuerst: warmes granite (tier 2) schlaegt kaltes qwen", d["node"] == "big" and d["model"] == "granite4.2:8b" and d["warm"] is True,
              f"{d} geladen={list(state()['nodes']['big']['loaded'])}")   # visible immediately on failure whether prewarm has already fetched qwen
        st, txt = chat("strikt:latest", ctx=16384)
        d = route()
        check("strikt (latency_first=false): tier0 qwen trotz Kaltstart", d["model"] == "qwen3-coder:30b" and d["tier"] == 0, str(d))
        for _ in range(10):   # ~7 s with running heartbeats: prewarm on free (6 s) fetches the rank-1 model back
            hb("big", 3, 8000, 24600); time.sleep(0.7)
        pre = [d for d in state()["decisions"] if d["event"] == "prewarm" and d.get("reason") == "free"]
        check("prewarm nach busy->free: Rang-1 wieder vorgeladen", any(p["node"] == "big" for p in pre), str(pre[-2:]))
        st, txt = chat("standard:latest", ctx=16384)
        d = route()
        check("standard nach prewarm -> tier0 qwen warm", d["node"] == "big" and d["tier"] == 0 and d["warm"] is True, str(d))

        # Residency: a specifically requested foreign model (gpt-oss, not rank 1 in any role) evicts qwen (Fake-Ollama
        # throws out the oldest at >31.8 GiB: granite, then qwen). Without the rule, qwen would stay cold and "warm first"
        # would take the fallback tier; with it, qwen returns after residency_idle_s (3 s in test) - but only,
        # because the evictor has not been requested since then.
        # Model switch: while gpt-oss is loading (slow_load 2.5 s, qwen counts as loaded until next poll), a
        # second request for the same model must not be rejected with 503 'no node', but wait for the load.
        hb("big", 2, 26600, 6000)   # 5.9 GiB free: without qwen in /api/ps, the budget for gpt-oss (12.8) is insufficient -> previously 503
        first = {}
        th = threading.Thread(target=lambda: first.update(zip(("st", "txt"), chat("gpt-oss:20b", ctx=8192, slow_load=6), strict=True)))
        th.start(); time.sleep(2.5)   # Poll saw the eviction, gpt-oss not yet there
        st2, txt2 = chat("gpt-oss:20b", ctx=8192)
        th.join()
        waits = [d for d in state()["decisions"] if d.get("event") == "wait_load"]
        check("Modellwechsel: zweite Anfrage wartet auf das Laden statt 503", first.get("st") == 200 and st2 == 200 and waits and waits[-1]["model"] == "gpt-oss:20b",
              f"erste {first.get('st')} zweite {st2} {txt2[:80] if st2 != 200 else ''} wait_load {len(waits)}")
        st, txt = chat("gpt-oss:20b", ctx=8192)
        time.sleep(0.8)   # Cold start triggers an immediate poll; wait briefly until /api/ps is read
        n = state()["nodes"]["big"]
        check("konkretes Fremdmodell verdraengt qwen (Vorbedingung)", st == 200 and "gpt-oss:20b" in n["loaded"] and "qwen3.6:35b-a3b" not in n["loaded"],
              f"geladen {list(n['loaded'])}")
        for _ in range(12):   # Heartbeats keep the node free/gpu_known; rule checks every second, idle 3 s
            hb("big", 3, 18000, 14600); time.sleep(0.6)   # 17.6 GiB used: gpt-oss 12 + Desktop, no foreign VRAM
            if state()["nodes"]["big"]["loaded"].get("qwen3.6:35b-a3b"):
                break
        n = state()["nodes"]["big"]
        res = [p for p in state()["decisions"] if p["event"] == "prewarm" and p.get("reason") == "residency"]
        check("Residenz: Rang-1 nach Leerlauf des Verdraengers wieder vorgewaermt", "qwen3.6:35b-a3b" in n["loaded"] and any(p["node"] == "big" for p in res),
              f"geladen {list(n['loaded'])}, prewarm-residency {len(res)}")
        st, txt = chat("standard:latest", ctx=16384)
        d = route()
        check("... und standard laeuft wieder warm auf tier0", d["node"] == "big" and d["tier"] == 0 and d["warm"] is True, str(d))

        # Warm-Fit: an already loaded model needs no extra VRAM. The heartbeat right after a response
        # shows little free VRAM (compute buffer); with budget check (1.27 + 20.6 - 1 = 20.9 < 21.5 GiB) the
        # warm node would fail and the request would go to the fallback tier - this happened on 2026-09-10 for the second
        # voice round on gpu-laptop/gemma instead of the warm qwen.
        hb("big", 2, 31300, 1300)
        st, txt = chat("standard:latest", ctx=16384)   # qwen is loaded with 16384 (last request), so warm for <= 16384
        d = route()
        check("Warm-Fit: geladenes Modell wird trotz knappem freiem VRAM genommen", st == 200 and d["node"] == "big" and d["tier"] == 0 and d["warm"] is True, str(d))

        # --- Tier 1: Request semantics (design/roadmap.md). The client describes (routing block, requirements from the
        # request), the router decides and states in the response what it did. ---
        hb("big", 3, 8000, 24600); time.sleep(0.3)   # wieder normal viel freies VRAM
        msg = [{"role": "user", "content": "hi"}]
        body = {"model": "standard:latest", "messages": msg, "stream": False,
                "routing": {"min_context": 40000, "request_id": "req-42", "session_id": "sess-1"}}
        REQ_T = time.time(); st, raw = http(R + "/api/chat", body)
        h1 = dict(LAST_HEADERS)   # before route()/state(), which overwrite LAST_HEADERS
        j = json.loads(raw) if st == 200 else {}
        ri = j.get("routing") or {}
        d = route()
        check("routing.min_context 40000: granite-Stufen (32k/16k) uebersprungen, tier0 qwen@65536",
              st == 200 and d["tier"] == 0 and d["ctx"] == 65536 and len(ri.get("skipped", [])) == 2
              and all("min_context" in s["reason"] for s in ri.get("skipped", [])), f"{st} {d} {ri}")
        check("Routing-Info im Body (routing-Block gesendet) + Header, request_id gespiegelt",
              ri.get("request_id") == "req-42" and ri.get("node") == "big" and ri.get("model") == "qwen3.6:35b-a3b"
              and h1.get("X-Skirnir-Request-Id") == "req-42" and h1.get("X-Skirnir-Node") == "big"
              and h1.get("X-Skirnir-Tier") == "0", f"{ri} {h1.get('X-Skirnir-Request-Id')}")
        body["routing"] = {"session_id": "sess-1", "min_context": 40000}
        REQ_T = time.time(); st, raw = http(R + "/api/chat", body)
        ri = (json.loads(raw) if st == 200 else {}).get("routing") or {}
        check("session_id: Folgeanfrage bleibt per Affinitaet auf dem warmen Knoten", st == 200 and ri.get("reason") == "affinity"
              and ri.get("node") == "big" and ri.get("session_id") == "sess-1" and ri.get("request_id", "").startswith("skirnir-"), str(ri))
        st, raw = chat("standard:latest", ctx=16384, stream=False)
        check("ohne routing-Block: Body wie bisher (kein routing), Header trotzdem", st == 200 and "routing" not in json.loads(raw)
              and LAST_HEADERS.get("X-Skirnir-Node") == "big" and LAST_HEADERS.get("X-Skirnir-Model") == "qwen3.6:35b-a3b", f"{st} {list(json.loads(raw))}")
        st, raw = chat("workload:standard", ctx=16384, stream=False)
        h1 = dict(LAST_HEADERS)
        d = route()
        check("workload:standard ist ein Alias der Rolle standard", st == 200 and d["role"] == "standard" and d["node"] == "big", f"{st} {d}")
        # derived requirements: tools in request, gpt-oss cannot (in Fake) -> specific model 400
        tool = [{"type": "function", "function": {"name": "f", "description": "x", "parameters": {"type": "object", "properties": {}}}}]
        st, raw = http(R + "/api/chat", {"model": "gpt-oss:20b", "messages": msg, "stream": False, "tools": tool})
        check("konkretes Modell ohne Faehigkeit tools -> 400 mit Klartext", st == 400 and "tools" in raw.decode(), f"{st} {raw[:120]!r}")
        # Tool call in wrong dialect: Ollama discards it silently (empty content, no tool_calls).
        # The rescue tier fetches it back - measured 2026-09-15 on qwen3-coder:30b, 80 % loss at ~12k tokens.
        tool_t = [{"type": "function", "function": {"name": "get_time", "description": "Zeit",
                   "parameters": {"type": "object", "properties": {"tz": {"type": "string"}}}}}]
        st, raw = http(R + "/api/chat", {"model": "qwen3.6:35b-a3b", "messages": msg, "stream": False,
                                         "tools": tool_t, "broken_tool_call": True})
        jr = json.loads(raw) if st == 200 else {}
        tc = (((jr.get("message") or {}).get("tool_calls") or [{}])[0]).get("function", {})
        check("Tool-Call aus Text gerettet (nicht-streamend)",
              st == 200 and tc.get("name") == "get_time" and tc.get("arguments") == {"tz": "CET"},
              f"{st} {raw[:160]!r}")
        check("geretteter Call laesst keinen Prosa-Rest", (jr.get("message") or {}).get("content") == "",
              repr((jr.get("message") or {}).get("content"))[:60])
        st, raw = http(R + "/api/chat", {"model": "qwen3.6:35b-a3b", "messages": msg, "stream": True,
                                         "tools": tool_t, "broken_tool_call": True})
        zeilen = [json.loads(z) for z in raw.decode().splitlines() if z.strip()]
        text_ = "".join((z.get("message") or {}).get("content") or "" for z in zeilen)
        tcs_r = [z for z in zeilen if ((z.get("message") or {}).get("tool_calls"))]
        check("Stream: Tool-Call gerettet, kein Prosa-Leck beim Client",
              st == 200 and len(tcs_r) == 1 and text_ == "", f"{st} {text_[:80]!r}")
        check("Stream: Zaehler ueberleben die Rettung",
              any(z.get("done") and z.get("eval_count") == 12 for z in zeilen), raw.decode()[-120:])
        # Counter-test: with format, the tier is off - a JSON response is the desired result there
        st, raw = http(R + "/api/chat", {"model": "qwen3.6:35b-a3b", "messages": msg, "stream": False,
                                         "tools": tool_t, "format": {"type": "object"}, "broken_tool_call": True})
        jr = json.loads(raw) if st == 200 else {}
        check("mit format keine Rettung (Text bleibt Text)",
              st == 200 and not (jr.get("message") or {}).get("tool_calls")
              and "<function=" in ((jr.get("message") or {}).get("content") or ""),
              f"{st} {raw[:120]!r}")
        # Role: all tiers fall away -> 400 (request error), not 503 (capacity)
        st, raw = http(R + "/api/chat", {"model": "strikt:latest", "messages": msg, "stream": False, "routing": {"require": ["vision"]}})
        check("routing.require vision: keine Stufe der Rolle kann es -> 400", st == 400 and "vision" in raw.decode(), f"{st} {raw[:120]!r}")
        # Role strict (coder -> granite) with think: coder does not think (Fake) -> tier 0 skipped, granite@16384 serves
        st, raw = http(R + "/api/chat", {"model": "strikt:latest", "messages": msg, "stream": False, "think": True, "routing": {}})
        ri = (json.loads(raw) if st == 200 else {}).get("routing") or {}
        check("think in Rolle strikt: coder-Stufe uebersprungen (fehlende Faehigkeit thinking), granite bedient",
              st == 200 and ri.get("model") == "granite4.2:8b" and ri.get("tier") == 1 and len(ri.get("skipped", [])) == 1
              and "thinking" in ri["skipped"][0]["reason"] and ri.get("required") == ["thinking"], f"{st} {ri}")
        # granite unloaded from big again, so the unload test below only finds qwen
        http("https://127.0.0.1:21001/api/generate", {"model": "granite4.2:8b", "keep_alive": 0}, headers={"X-Router-Token": "testtoken"}, auth=False)
        time.sleep(1.2)
        # Catalog override: gpt-oss structured:false -> format schema rejected before Ollama returns 500
        st, raw = http(R + "/api/chat", {"model": "gpt-oss:20b", "messages": msg, "stream": False, "format": {"type": "object"}})
        check("Katalog-Override capabilities.structured=false -> 400 bei format-Schema", st == 400 and "structured" in raw.decode(), f"{st} {raw[:120]!r}")
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {"require": ["telepathie"]}})
        check("unbekannte Faehigkeit im routing-Block -> 400", st == 400 and "telepathie" in raw.decode(), f"{st} {raw[:120]!r}")
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {"foo": 1}})
        check("unbekanntes Feld im routing-Block -> 400", st == 400 and "foo" in raw.decode(), f"{st} {raw[:120]!r}")
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "stream": False,
                                         "messages": [{"role": "user", "content": "hi", "images": ["AA==", "AA==", "AA=="]}]})
        check("Limit max_images 2: drei Bilder -> 413", st == 413 and "images" in raw.decode(), f"{st} {raw[:120]!r}")
        # /v1: same routing block, info in object and header
        st, raw = http(R + "/v1/chat/completions", {"model": "standard", "messages": msg, "routing": {"request_id": "oa-7"}})
        j = json.loads(raw) if st == 200 else {}
        check("/v1: routing-Block angenommen, Routing-Info im chat.completion + Header", st == 200
              and (j.get("routing") or {}).get("request_id") == "oa-7" and j.get("object") == "chat.completion"
              and LAST_HEADERS.get("X-Skirnir-Request-Id") == "oa-7", f"{st} {list(j)} {j.get('routing')}")
        st, raw = http(R + "/v1/chat/completions", {"model": "standard", "messages": msg})
        j = json.loads(raw) if st == 200 else {}
        check("/v1 ohne routing-Block: Objekt unveraendert", st == 200 and "routing" not in j, f"{st} {list(j)}")
        caps = state()["capabilities_effective"]
        check("/admin/state: effektive Faehigkeiten (Ollama + Katalog-Override)", "structured" not in caps.get("gpt-oss:20b", ["structured"])
              and "structured" in caps.get("qwen3.6:35b-a3b", []) and "tools" not in caps.get("gpt-oss:20b", ["tools"])
              and "thinking" not in caps.get("qwen3-coder:30b", ["thinking"]), str({k: caps.get(k) for k in ("gpt-oss:20b", "qwen3-coder:30b")}))

        # --- Tier 3: Scheduler - Score, Statistics, Circuit Breaker, Admission Control with priorities ---
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {}})
        ri = (json.loads(raw) if st == 200 else {}).get("routing") or {}
        cands = ri.get("candidates") or []
        check("Score: Kandidaten der Stufe mit Punkten in der Routing-Info, gewaehlt = hoechster (warm)", st == 200 and cands
              and cands[0]["node"] == ri.get("node") and cands[0]["warm"] is True and cands[0]["score"] > 100, str(cands))
        pe = state()["perf"].get("qwen3.6:35b-a3b@big") or {}
        check("Statistik: EWMA tok/s und Ergebniszaehler je Modell@Knoten", (pe.get("ewma") or {}).get("gen_tps") and (pe.get("outcomes") or {}).get("ok", 0) >= 1
              and ri.get("priority") == "normal", str({k: pe.get(k) for k in ("ewma", "outcomes")}))
        # Circuit Breaker: three backend errors (500 before first byte) -> big OPEN (2 s in test); requests go to small,
        # although qwen is warm on big; after open_s a probe (half_open) -> success -> closed
        for _ in range(3):
            st, raw = http(R + "/api/chat", {"model": "qwen3.6:35b-a3b", "messages": msg, "stream": False, "fail_status": 500})
        nb = state()["nodes"]["big"]
        check("Breaker: nach 3 Fehlern ist big OPEN, der Client bekam 503", st == 503 and nb.get("breaker") == "open", f"{st} {nb.get('breaker')}")
        st, raw = chat("standard:latest", ctx=4096, stream=False)
        d = route()
        check("Breaker OPEN: standard@4096 geht auf small (granite), obwohl qwen auf big warm ist", st == 200 and d["node"] == "small", str(d))
        pe = state()["perf"].get("qwen3.6:35b-a3b@big") or {}
        check("Statistik zaehlt die Fehler", (pe.get("outcomes") or {}).get("error", 0) >= 3, str(pe.get("outcomes")))
        time.sleep(2.2)
        st, raw = chat("standard:latest", ctx=4096, stream=False)
        d = route()
        nb = state()["nodes"]["big"]
        check("Breaker HALF_OPEN: Probe auf big gelingt -> CLOSED, Route wieder big/qwen warm", st == 200 and d["node"] == "big" and nb.get("breaker") == "closed", f"{d} {nb.get('breaker')}")
        ev = [x["state"] for x in state()["decisions"] if x["event"] == "breaker"]
        check("Breaker-Ereignisse protokolliert (open, half_open, closed)", ev[-3:] == ["open", "half_open", "closed"], str(ev))
        # Admission: big max_inflight 1 (Policy). One long request runs (1.5 s); batch and interactive wait;
        # interactive is admitted first. deadline_ms 300 -> 503 instead of waiting. Then Policy back.
        http(C + f"/admin/nodes/{big['fp']}/policy", {"max_inflight": 1})
        order = []

        def worker(tag, body):
            st_, raw_ = http(R + "/api/chat", body, timeout=30)
            order.append((tag, st_, (json.loads(raw_).get("routing") or {}) if st_ == 200 else raw_[:80]))
        long_body = {"model": "standard:latest", "messages": msg, "stream": False, "sleep_s": 1.5, "routing": {"request_id": "long"}}
        t_l = threading.Thread(target=worker, args=("long", long_body)); t_l.start(); time.sleep(0.4)
        t_b = threading.Thread(target=worker, args=("batch", {"model": "standard:latest", "messages": msg, "stream": False,
                                                              "routing": {"priority": "batch", "request_id": "b"}})); t_b.start(); time.sleep(0.2)
        t_i = threading.Thread(target=worker, args=("interactive", {"model": "standard:latest", "messages": msg, "stream": False,
                                                                    "routing": {"priority": "interactive", "request_id": "i"}})); t_i.start()
        time.sleep(0.3)
        adm = state()["admission"]
        check("Admission: zwei Anfragen warten, waehrend big (max_inflight 1) belegt ist", adm["waiting"] == 2 and adm["by_node"].get("big") == 2, str(adm))
        for t in (t_l, t_b, t_i):
            t.join(25)
        seq = [tag for tag, _, _ in order]
        check("Prioritaet: interactive wird vor batch zugelassen, alle 200", seq == ["long", "interactive", "batch"] and all(s_ == 200 for _, s_, _ in order),
              str([(t_, s_) for t_, s_, _ in order]))
        qi = next((ri_ for tag, _, ri_ in order if tag == "interactive"), {})
        check("Routing-Info: priority + queued_ms der Wartenden", isinstance(qi, dict) and qi.get("priority") == "interactive" and qi.get("queued_ms", 0) > 200,
              str({k: qi.get(k) for k in ("priority", "queued_ms")} if isinstance(qi, dict) else qi))
        t_l2 = threading.Thread(target=worker, args=("long2", long_body)); t_l2.start(); time.sleep(0.4)
        t0 = time.time()
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {"deadline_ms": 300}})
        dt = time.time() - t0
        check("deadline_ms 300 bei belegtem Knoten -> 503 nach ~0.3 s mit Klartext", st == 503 and dt < 1.5 and "deadline" in raw.decode(), f"{st} {dt:.2f}s {raw[:110]!r}")
        t_l2.join(25)
        http(C + f"/admin/nodes/{big['fp']}/policy", {"max_inflight": None})
        check("Policy max_inflight zurueck auf Default", state()["nodes"]["big"]["max_inflight"] == 4, str(state()["nodes"]["big"]["max_inflight"]))
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {"priority": "interactive"}},
                       headers={"Authorization": "Bearer batchy-token"})
        ri = (json.loads(raw) if st == 200 else {}).get("routing") or {}
        check("client_auth.max_priority: interactive wird fuer Client batchy auf batch gekappt", st == 200 and ri.get("priority") == "batch" and ri.get("client") == "batchy",
              str({k: ri.get(k) for k in ("priority", "client")}))
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {"priority": "dringend"}})
        check("unbekannte Prioritaet -> 400", st == 400 and "priority" in raw.decode(), f"{st} {raw[:100]!r}")

        # --- Tier 4: Observability - /metrics (Prometheus text) and Usage ---
        st, raw = http(C + "/metrics")
        txt = raw.decode()
        check("/metrics: 200, Prometheus-Text mit Anfragen, Knoten, Histogramm, Tokens, Ereignissen", st == 200
              and 'skirnir_requests_total{' in txt and 'role="standard"' in txt and 'skirnir_node_state{node="big",state="free"} 1' in txt
              and "skirnir_request_duration_seconds_bucket{" in txt and 'skirnir_tokens_total{' in txt and 'kind="completion"' in txt
              and 'skirnir_events_total{event="breaker",node="big",state="open"} 1' in txt and 'skirnir_node_breaker{node="big",state="closed"} 1' in txt
              and 'skirnir_perf_gen_tps{model="qwen3.6:35b-a3b",node="big"}' in txt and 'skirnir_role_ready{role="standard"} 1' in txt,
              " | ".join(l for l in txt.splitlines() if l.startswith(("skirnir_requests_total", "skirnir_events_total{event=\"breaker")))[:400])
        st, raw = http(C + "/metrics", auth=False)
        check("/metrics verlangt Basic Auth wie /admin", st == 401, str(st))
        u = json.loads(http(C + "/admin/usage")[1])
        today = u["today"]
        check("/admin/usage: heute Anfragen und Tokens, Client batchy gezaehlt", today["requests"] > 10 and today["completion_tokens"] > 0
              and (today["clients"].get("batchy") or {}).get("requests", 0) >= 1 and "standard" in today["clients"]["batchy"]["roles"],
              str({k: today.get(k) for k in ("requests", "prompt_tokens", "completion_tokens", "errors")}) + f" batchy={today['clients'].get('batchy', {}).get('requests')}")
        s_ = state()
        check("/admin/state: usage_today + Rollen-Prioritaet", s_["usage_today"]["requests"] == today["requests"] and s_["roles"]["standard:latest"]["priority"] == "normal", str(s_["usage_today"]))
        ha_ = json.loads(http(C + "/admin/ha")[1])
        check("HA-Snapshot: requests_today/tokens_today", ha_["requests_today"] == today["requests"] and ha_["tokens_today"] == today["prompt_tokens"] + today["completion_tokens"],
              str({k: ha_.get(k) for k in ("requests_today", "tokens_today", "errors_today")}))

        # --- Tier 6: Operations - Config schema, Idempotency, Canary, Shadow, Supply Chain ---
        router_py = os.path.join(HERE, "..", "router", "router.py")
        rc = subprocess.run([PY, router_py, "--check", os.path.join(HERE, "test-config.yaml"), "--pure"], capture_output=True, text=True, cwd=HERE)
        check("router.py --check: Testkonfiguration gueltig (rc 0)", rc.returncode == 0 and "configuration ok" in rc.stdout, (rc.stdout + rc.stderr)[-200:])
        bad = os.path.join(HERE, "bad-config.yaml")
        open(bad, "w", encoding="utf-8").write(open(os.path.join(HERE, "test-config.yaml"), encoding="utf-8").read().replace("  limits: {", "  limitz: {"))
        rc = subprocess.run([PY, router_py, "--check", bad, "--pure"], capture_output=True, text=True, cwd=HERE)
        os.remove(bad)
        check("--check: Tippfehler router.limitz -> rc 2, benannt, mit Vorschlag 'limits'", rc.returncode == 2 and "router.limitz" in rc.stdout and "'limits'" in rc.stdout, (rc.stdout + rc.stderr)[-220:])
        # Idempotency: repetition with same key = same response, no second inference
        body = {"model": "standard:latest", "messages": msg, "stream": False}
        REQ_T = time.time()
        st1, raw1 = http(R + "/api/chat", body, headers={"Idempotency-Key": "idem-1"}); hh1 = dict(LAST_HEADERS)
        st2, raw2 = http(R + "/api/chat", body, headers={"Idempotency-Key": "idem-1"}); hh2 = dict(LAST_HEADERS)
        routes_ = [d for d in state()["decisions"] if d["event"] == "route" and d.get("t", 0) >= REQ_T]
        check("Idempotency-Key: Wiederholung liefert dieselbe Antwort ohne zweite Inferenz, Replay-Header", st1 == st2 == 200 and raw1 == raw2
              and len(routes_) == 1 and hh2.get("X-Skirnir-Idempotent-Replay") == "1" and "X-Skirnir-Idempotent-Replay" not in hh1,
              f"{st1}/{st2} routen={len(routes_)} replay={hh2.get('X-Skirnir-Idempotent-Replay')}")
        st3, raw3 = http(R + "/api/chat", {**body, "stream": True}, headers={"Idempotency-Key": "idem-2"})
        check("Idempotency: Streams werden nicht gecacht", st3 == 200 and "X-Skirnir-Idempotent-Replay" not in LAST_HEADERS, str(st3))
        # Canary 100 %: role canary (granite) runs on canary tier qwen; Shadow 100 %: then granite@4096 in shadow
        REQ_T = time.time()
        st, raw = http(R + "/api/chat", {"model": "kanarie:latest", "messages": msg, "stream": False, "routing": {}})
        ri = (json.loads(raw) if st == 200 else {}).get("routing") or {}
        check("Canary 100 %: kanarie laeuft auf granite (Canary-Stufe) statt qwen, Info canary=true", st == 200 and ri.get("model") == "granite4.2:8b" and ri.get("canary") is True,
              str({k: ri.get(k) for k in ("model", "canary", "tier")}))
        ev = None
        for _ in range(25):
            time.sleep(0.3)
            ev = next((d for d in reversed(state()["decisions"]) if d["event"] == "shadow" and d.get("t", 0) >= REQ_T), None)
            if ev:
                break
        pe = (state()["perf"].get(f"qwen3.6:35b-a3b@{ev['node']}") or {}) if ev and ev.get("node") else {}
        ushadow = (json.loads(http(C + "/admin/usage")[1])["today"]["clients"].get("shadow") or {}).get("requests", 0)
        check("Shadow 100 %: Schattenlauf qwen protokolliert (ok), perf kind shadow, Usage-Client shadow", ev is not None and ev.get("status") == "ok"
              and (pe.get("last") or {}).get("kind") == "shadow" and ushadow >= 1, f"{ev} usage_shadow={ushadow}")
        http("https://127.0.0.1:21001/api/generate", {"model": "granite4.2:8b", "keep_alive": 0}, headers={"X-Router-Token": "testtoken"}, auth=False)
        time.sleep(1.2)
        s_ = state()
        sc = (s_.get("supply_chain") or {}).get("big") or {}
        check("Supply Chain: Ollama-Version je Knoten, Modell-Digests, Manifest-Status im State", sc.get("ollama_version") == "0.0.0-fake"
              and bool((sc.get("models", {}).get("qwen3.6:35b-a3b") or {}).get("digest")) and "present" in (s_.get("build") or {}),
              f"version={sc.get('ollama_version')} build={s_.get('build')}")

        # --- Tier 5: Cloud as tier in roles (local first; data class, credential scan, opt-out, budget) ---
        def rinfo(raw_, st_):
            return (json.loads(raw_) if st_ == 200 else {}).get("routing") or {}
        cl = state()["cloud"]
        check("Cloud-Anbieter konfiguriert: Schluessel geladen, Art, Budget", cl["testcloud"]["key"] and cl["testcloud"]["kind"] == "openai"
              and cl["testclaude"]["key"] and cl["testclaude"]["kind"] == "anthropic" and cl["testcloud"]["budget_month_chf"] == 1.9,
              str({k: {x: v[x] for x in ("key", "kind", "budget_month_chf")} for k, v in cl.items()}))
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {}})
        ri = rinfo(raw, st)
        check("wolke: lokal zuerst (qwen warm), Cloud-Stufe nur Ausweich", st == 200 and ri.get("model") == "qwen3.6:35b-a3b" and ri.get("cloud") is False, str({k: ri.get(k) for k in ("model", "node", "cloud")}))
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud", "data_class": "internal", "request_id": "cloud-1"}})
        j = json.loads(raw) if st == 200 else {}
        ri = j.get("routing") or {}
        hh = dict(LAST_HEADERS)
        check("execution cloud + Datenklasse internal: Antwort ueber den OpenAI-Adapter im Ollama-Format, Header", st == 200
              and ri.get("node") == "cloud:testcloud" and ri.get("model") == "testcloud:mini" and ri.get("cloud") is True and ri.get("data_class") == "internal"
              and (j.get("message") or {}).get("content") == "Hallo aus der Wolke" and j.get("eval_count") == 30 and j.get("prompt_eval_count") == 20
              and j.get("model") == "wolke:latest" and hh.get("X-Skirnir-Node") == "cloud:testcloud", f"{st} {ri} {list(j)}")
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud"}})
        ri = rinfo(raw, st)
        check("Default-Datenklasse personal: Cloud-Stufe mit Grund uebersprungen, lokal bedient", st == 200 and ri.get("model") == "qwen3.6:35b-a3b"
              and any("data class personal" in s_["reason"] for s_ in ri.get("skipped", [])), str(ri.get("skipped")))
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": [{"role": "user", "content": "mein key ist sk-abcdefghijklmnopqrstuvwxyz0123456789"}], "stream": False,
                                         "routing": {"execution": "cloud", "data_class": "public"}})
        ri = rinfo(raw, st)
        check("Credential-Scan: Prompt mit Schluessel geht nicht in die Cloud (lokal bedient, Grund sichtbar)", st == 200 and ri.get("model") == "qwen3.6:35b-a3b"
              and any("credential_scan" in s_["reason"] for s_ in ri.get("skipped", [])), str(ri.get("skipped")))
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud", "data_class": "public"}},
                       headers={"Authorization": "Bearer lokal-token"})
        ri = rinfo(raw, st)
        check("Client-Opt-out (cloud: false): trotz execution cloud lokal", st == 200 and ri.get("model") == "qwen3.6:35b-a3b"
              and any("opt-out" in s_["reason"] for s_ in ri.get("skipped", [])), str(ri.get("skipped")))
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "local", "data_class": "public"}})
        ri = rinfo(raw, st)
        check("routing.execution local: Cloud-Stufe uebersprungen", st == 200 and ri.get("model") == "qwen3.6:35b-a3b" and any("execution: local" in s_["reason"] for s_ in ri.get("skipped", [])), str(ri.get("skipped")))
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": [{"role": "user", "content": "was ist das?", "images": ["AA=="]}], "stream": False,
                                         "routing": {"data_class": "internal"}})
        ri = rinfo(raw, st)
        check("Fall 2 - Faehigkeit fehlt lokal (vision): Cloud-Stufe bedient, qwen als uebersprungen gemeldet", st == 200 and ri.get("model") == "testcloud:mini"
              and any("vision" in s_["reason"] for s_ in ri.get("skipped", [])), str({k: ri.get(k) for k in ("model", "skipped")}))
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": True, "routing": {"execution": "cloud", "data_class": "internal"}})
        lines_ = [json.loads(l) for l in raw.decode().splitlines() if l.strip()] if st == 200 else []
        content_ = "".join((l.get("message") or {}).get("content", "") for l in lines_)
        check("Cloud-Stream (OpenAI SSE -> Ollama ndjson): Text vollstaendig, done mit eval_count + Routing-Info", st == 200 and content_ == "Hallo aus der Wolke"
              and lines_ and lines_[-1].get("done") is True and lines_[-1].get("eval_count") == 30 and (lines_[-1].get("routing") or {}).get("cloud") is True,
              f"{st} {content_!r} {lines_[-1] if lines_ else None}")
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "tools": tool, "routing": {"execution": "cloud", "data_class": "internal"}})
        j = json.loads(raw) if st == 200 else {}
        tcs_ = ((j.get("message") or {}).get("tool_calls") or [{}])
        check("Tool-Call ueber die Cloud: arguments als Objekt im Ollama-Format", st == 200 and tcs_[0].get("function", {}).get("name") == "get_time"
              and tcs_[0].get("function", {}).get("arguments") == {"tz": "CET"}, str(j.get("message")))
        st, raw = http(R + "/api/chat", {"model": "wolke2:latest", "messages": [{"role": "system", "content": "Du bist knapp."}, {"role": "user", "content": "hi"}], "stream": False,
                                         "routing": {"data_class": "internal"}})
        j = json.loads(raw) if st == 200 else {}
        ri = j.get("routing") or {}
        check("Anthropic-Adapter: wolke2 (Cloud zuerst) antwortet, usage uebernommen", st == 200 and ri.get("node") == "cloud:testclaude"
              and (j.get("message") or {}).get("content") == "Hallo von Claude" and j.get("prompt_eval_count") == 25 and j.get("eval_count") == 35, f"{st} {ri} {j.get('message')}")
        st, raw = http(R + "/api/chat", {"model": "wolke2:latest", "messages": msg, "stream": True, "routing": {"data_class": "internal"}})
        lines_ = [json.loads(l) for l in raw.decode().splitlines() if l.strip()] if st == 200 else []
        content_ = "".join((l.get("message") or {}).get("content", "") for l in lines_)
        check("Anthropic-Stream (SSE-Events -> Ollama ndjson)", st == 200 and content_ == "Hallo von Claude" and lines_ and lines_[-1].get("eval_count") == 35, f"{st} {content_!r}")
        st, raw = http(R + "/v1/chat/completions", {"model": "wolke", "messages": msg, "routing": {"execution": "cloud", "data_class": "internal"}})
        oa_ = json.loads(raw) if st == 200 else {}
        check("/v1 ueber die Cloud: chat.completion mit Rollenname, usage, Routing-Info", st == 200 and oa_.get("object") == "chat.completion" and oa_.get("model") == "wolke:latest"
              and (oa_.get("choices") or [{}])[0].get("message", {}).get("content") == "Hallo aus der Wolke" and (oa_.get("usage") or {}).get("total_tokens") == 50
              and (oa_.get("routing") or {}).get("node") == "cloud:testcloud", raw.decode()[:200])
        u_ = json.loads(http(C + "/admin/usage")[1])["today"]
        spent = u_["cost_chf"]
        check("Usage: Cloud-Kosten je Aufruf gezaehlt (0.32 CHF bei Testpreisen), im Tagesbudget sichtbar", 1.5 < spent < 1.7 and (u_["clients"]["-"]["models"].get("testcloud:mini") or {}).get("cost_chf", 0) > 1.0,
              f"cost_chf heute={spent}")
        hit = None
        for _ in range(4):
            st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud", "data_class": "internal"}})
            ri = rinfo(raw, st)
            if ri.get("model") == "qwen3.6:35b-a3b":
                hit = [s_["reason"] for s_ in ri.get("skipped", []) if "monthly budget" in s_["reason"]]
                break
        check("Budget 1.9 CHF erschoepft: Cloud-Stufe faellt weg (Grund), lokal bedient", bool(hit), str(hit or ri))
        ha_ = json.loads(http(C + "/admin/ha")[1])
        check("HA: Budget-Problem gemeldet, Cloud-Kosten im Snapshot", any("Cloud testcloud" in p_ for p_ in ha_["problems"]) and ha_["cloud_spend_month_chf"] >= 1.9
              and ha_["cloud"]["testcloud"]["budget_percent"] >= 100, str(ha_["problems"]) + f" spend={ha_['cloud_spend_month_chf']}")
        txt = http(C + "/metrics")[1].decode()
        check("/metrics: Cloud-Ausgaben und Budget je Anbieter", 'skirnir_cloud_spend_month_chf{provider="testcloud"}' in txt and 'skirnir_cloud_budget_month_chf{provider="testcloud"} 1.9' in txt
              and 'skirnir_cloud_cost_chf_total{' in txt, " | ".join(l for l in txt.splitlines() if l.startswith("skirnir_cloud_"))[:300])
        st, raw = http(R + "/api/chat", {"model": "testcloud:mini", "messages": msg, "stream": False})
        check("konkreter Cloud-Modellname ist NICHT direkt aufrufbar (nur ueber Rollen)", st == 404, f"{st}")
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"data_class": "geheimnis"}})
        check("unbekannte Datenklasse -> 400", st == 400 and "data_class" in raw.decode(), f"{st}")

        # --- Tier 7: Settings via UI (settings block in roles.yaml, allowlist settings.py), cloud models in catalog, logo ---
        cfg7 = json.loads(http(C + "/admin/config")[1])
        items7 = {i["path"]: i for i in cfg7.get("settings", {}).get("items", [])}
        bud7 = items7.get("router.cloud.providers.testcloud.budget_month_chf") or {}
        check("GET /admin/config: editierbare Einstellungen mit Wert/Basis/Gruppe (Budget je Anbieter, Client-Datenklasse, Score); Endpunkte nicht dabei",
              bud7.get("value") == 1.9 and bud7.get("group") == "Cloud-Anbieter" and bud7.get("entity") == "testcloud"
              and "router.client_auth.clients.tester.data_class" in items7 and items7["modes.score.warm"]["set"] is False and items7["modes.score.warm"]["default"] == 100.0
              and "router.cloud.providers.testcloud.base_url" not in items7 and "router.client_auth.clients.tester.token_sha256" not in items7, str(sorted(items7)[:5]))
        st, raw = http(C + "/admin/config", {"settings": {"router.cloud.providers.testcloud.budget_month_chf": 50, "router.cloud.providers.testcloud.warn_at_percent": 90}}, method="PUT")
        s7 = state()["cloud"]["testcloud"]
        check("PUT settings: Budget live geaendert (1.9 -> 50 CHF), Anbieter wieder unter der Grenze", st == 200 and s7["budget_month_chf"] == 50.0 and s7["budget_percent"] < 10, f"{st} {s7}")
        st, raw = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud", "data_class": "internal"}})
        ri = rinfo(raw, st)
        check("nach der Budget-Erhoehung bedient die Cloud-Stufe wieder", st == 200 and ri.get("node") == "cloud:testcloud", str({k: ri.get(k) for k in ("node", "skipped")}))
        st, raw = http(C + "/admin/config", {"settings": {"router.cloud.providers.testcloud.base_url": "http://evil"}}, method="PUT")
        check("nicht freigegebener Pfad (base_url) -> 400", st == 400 and "cannot be changed in the UI" in raw.decode(), raw.decode()[:120])
        st, raw = http(C + "/admin/config", {"settings": {"modes.admission.max_inflight_default": 0}}, method="PUT")
        check("Wert ausserhalb des Bereichs -> 400", st == 400 and "between" in raw.decode(), raw.decode()[:120])
        st, raw = http(C + "/admin/config", {"settings": {"router.cloud.providers.gibtsnicht.enabled": False}}, method="PUT")
        check("unbekannter Anbieter -> 400 (die UI legt keine Anbieter an)", st == 400, raw.decode()[:120])
        st, raw = http(C + "/admin/config", {"settings": {"router.client_auth.clients.tester.data_class": "internal"}}, method="PUT")
        st2, raw2 = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud"}}, headers={"Authorization": "Bearer tester-token"})
        ri = rinfo(raw2, st2)
        check("Client-Datenklasse per UI (tester: internal): Cloud ohne routing.data_class erlaubt", st == 200 and st2 == 200 and ri.get("node") == "cloud:testcloud" and ri.get("data_class") == "internal",
              f"{st} {raw.decode()[:80]} {ri.get('node')} {ri.get('data_class')} {ri.get('skipped')}")
        st, raw = http(C + "/admin/config", {"settings": {"router.client_auth.clients.tester.data_class": None}}, method="PUT")
        st2, raw2 = http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud"}}, headers={"Authorization": "Bearer tester-token"})
        ri = rinfo(raw2, st2)
        check("Reset (null): zurueck auf config.yaml, Datenklasse wieder personal", st == 200 and ri.get("model") == "qwen3.6:35b-a3b" and any("personal" in s_["reason"] for s_ in ri.get("skipped", [])), str(ri.get("skipped")))
        txt7 = open(os.path.join(HERE, "roles.yaml"), encoding="utf-8").read()
        check("roles.yaml traegt den settings-Block (Budget), der zurueckgesetzte Client-Eintrag ist weg", "budget_month_chf: 50" in txt7 and "tester.data_class" not in txt7, txt7[-300:])
        rc7 = subprocess.run([PY, os.path.join(HERE, "..", "router", "router.py"), "--check", os.path.join(HERE, "test-config.yaml")], cwd=HERE, capture_output=True, text=True)
        check("--check akzeptiert roles.yaml mit settings (Deploy-Pruefung auf dem CT)", rc7.returncode == 0 and "with roles.yaml" in rc7.stdout, (rc7.stdout + rc7.stderr)[-200:])
        st, raw = http(C + "/admin/config", {"settings": {"modes.score.errors": 42, "modes.keep_alive.busy": "7m", "router.openai.default_think": None}}, method="PUT")
        check("Score-Gewicht, keep_alive und Tristate live geaendert", st == 200 and state()["scheduler"]["score"]["errors"] == 42.0
              and json.loads(http(C + "/admin/config")[1])["modes"]["keep_alive"]["busy"] == "7m", raw.decode()[:100])
        st, raw = http(C + "/admin/config", {"settings": {"modes.keep_alive.busy": "bald"}}, method="PUT")
        check("keep_alive mit Unsinn -> 400", st == 400, raw.decode()[:100])
        st, raw = http(C + "/admin/config", {"settings": {"router.client_auth.mode": "enforce"}}, method="PUT")
        st2, _ = http(R + "/api/tags")
        check("client_auth.mode per Einstellungen dauerhaft auf enforce: ohne Identitaet 401", st == 200 and st2 == 401, f"{st} {st2}")
        http(C + "/admin/config", {"settings": {"router.client_auth.mode": None, "modes.score.errors": None, "modes.keep_alive.busy": None,
                                                 "router.cloud.providers.testcloud.budget_month_chf": None, "router.cloud.providers.testcloud.warn_at_percent": None}}, method="PUT")
        st2, _ = http(R + "/api/tags")
        check("alle Einstellungen zurueckgesetzt: observe, Budget 1.9, kein settings-Block mehr", st2 == 200 and state()["cloud"]["testcloud"]["budget_month_chf"] == 1.9
              and "\nsettings:" not in (open(os.path.join(HERE, "roles.yaml"), encoding="utf-8").read()), "")
        st, raw = http(C + "/admin/config", {"models": {"testcloud:maxi": {"cloud": "testcloud", "provider_model": "maxi-2", "price_chf_per_m": {"input": 2, "output": 8}, "context_tokens": 200000, "reasoning": True}}}, method="PUT")
        cfg7 = json.loads(http(C + "/admin/config")[1])
        check("Cloud-Modell per UI in den Katalog: Anbieter, Preise, Kontext; als UI-Modell markiert", st == 200 and (cfg7["models"].get("testcloud:maxi") or {}).get("price_chf_per_m", {}).get("output") == 8.0
              and "testcloud:maxi" in cfg7["ui_models"] and "testcloud:mini" not in cfg7["ui_models"], raw.decode()[:120])
        st, raw = http(C + "/admin/config", {"models": {"x:y": {"cloud": "nope"}}}, method="PUT")
        check("Cloud-Modell mit unbekanntem Anbieter -> 400", st == 400, raw.decode()[:100])
        roles7 = cfg7["roles"]; roles7["wolke"]["tiers"].append({"model": "testcloud:maxi", "num_ctx": 100000})
        st, raw = http(C + "/admin/config", {"roles": roles7}, method="PUT")
        st2, raw2 = http(C + "/admin/config", {"delete_models": ["testcloud:maxi"]}, method="PUT")
        check("UI-Modell in einer Rolle -> Loeschen abgelehnt", st == 200 and st2 == 400 and "roles" in raw2.decode(), f"{st} {raw.decode()[:80]} {st2} {raw2.decode()[:100]}")
        roles7["wolke"]["tiers"].pop()
        http(C + "/admin/config", {"roles": roles7}, method="PUT")
        st2, raw2 = http(C + "/admin/config", {"delete_models": ["testcloud:maxi", "testcloud:mini"]}, method="PUT")
        m7 = json.loads(http(C + "/admin/config")[1])["models"]
        check("UI-Modell geloescht, Katalogeintrag aus config.yaml bleibt", st2 == 200 and "testcloud:maxi" not in m7 and "testcloud:mini" in m7, raw2.decode()[:100])
        st, raw = http(C + "/skirnir.png")
        check("Logo /skirnir.png (PNG), Tab-Icon /favicon.png und /favicon.ico", st == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n"
              and http(C + "/favicon.png")[1][:4] == b"\x89PNG" and http(C + "/favicon.ico")[0] == 200)

        # --- Clients tab: create client, generate/rotate secret, source IP, delete (all in roles.yaml, config.yaml stays) ---
        import hashlib as _hl
        st, raw = http(C + "/admin/config", {"clients": {"pi-voice": {"roles": ["*"], "note": "Testclient"}}}, method="PUT")
        st2, raw2 = http(C + "/admin/clients/pi-voice/token", {}, method="POST")
        tok7 = (json.loads(raw2) if st2 == 200 else {}).get("token", "")
        ry = open(os.path.join(HERE, "roles.yaml"), encoding="utf-8").read()
        check("Client per UI angelegt, Secret erzeugt: Klartext in der Antwort, nur der sha256 in roles.yaml", st == 200 and st2 == 200 and len(tok7) >= 40
              and _hl.sha256(tok7.encode()).hexdigest() in ry and tok7 not in ry, f"{st} {raw.decode()[:80]} {st2}")
        rlog.seek(0); check("Secret steht nirgends im Router-Log", tok7 not in rlog.read())
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {}}, headers={"Authorization": "Bearer " + tok7})
        ri = rinfo(raw, st)
        check("neuer Client weist sich mit dem Secret aus", st == 200 and ri.get("client") == "pi-voice", str(ri.get("client")))
        cv = json.loads(http(C + "/admin/config")[1])["clients"]
        check("Clients-Sicht: Herkunft ui/config.yaml, Hash-Praefix, Rotationszeit, kein voller Hash", cv["pi-voice"]["source"] == "ui" and cv["tester"]["source"] == "config.yaml"
              and cv["pi-voice"]["has_token"] and len(cv["pi-voice"]["token_prefix"]) == 8 and cv["pi-voice"]["token_rotated"], str(cv.get("pi-voice")))
        st, raw = http(C + "/admin/clients/pi-voice/token", {}, method="POST")
        tok7b = json.loads(raw)["token"] if st == 200 else ""
        ri_old = rinfo(*http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {}}, headers={"Authorization": "Bearer " + tok7})[::-1])
        ri_new = rinfo(*http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {}}, headers={"Authorization": "Bearer " + tok7b})[::-1])
        check("Rotation: altes Secret gilt nicht mehr (observe: anonym bedient), neues gilt", st == 200 and tok7b and tok7b != tok7 and ri_old.get("client") is None and ri_new.get("client") == "pi-voice",
              f"alt={ri_old.get('client')} neu={ri_new.get('client')}")
        st, raw = http(C + "/admin/config", {"settings": {"router.client_auth.clients.pi-voice.ip": "127.0.0.1", "router.client_auth.clients.pi-voice.data_class": "internal",
                                                 "router.cloud.providers.testcloud.budget_month_chf": 50}}, method="PUT")   # Budget: the Cloud block above consumed 1.9 CHF
        ri = rinfo(*http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud"}})[::-1])
        check("Quell-IP + Datenklasse per UI: Anfrage ohne Header ist pi-voice und darf in die Cloud", st == 200 and ri.get("client") == "pi-voice" and ri.get("node") == "cloud:testcloud",
              f"{st} {raw.decode()[:80]} client={ri.get('client')} node={ri.get('node')} {ri.get('skipped')}")
        http(C + "/admin/config", {"settings": {"router.cloud.providers.testcloud.budget_month_chf": None}}, method="PUT")
        st, raw = http(C + "/admin/config", {"settings": {"router.client_auth.clients.pi-voice.ip": "nix"}}, method="PUT")
        check("ungueltige IP -> 400", st == 400 and "IP address" in raw.decode(), raw.decode()[:100])
        st, raw = http(C + "/admin/config", {"clients": {"pi-voice": {"token_sha256": "00"}}}, method="PUT")
        check("Hash direkt setzen ist nicht erlaubt (nur ueber /token)", st == 400, raw.decode()[:100])
        st, raw = http(C + "/admin/config", {"delete_clients": ["tester"]}, method="PUT")
        check("Client aus config.yaml ist per UI nicht loeschbar", st == 400 and "config.yaml" in raw.decode(), raw.decode()[:100])
        st, raw = http(C + "/admin/config", {"delete_clients": ["pi-voice"]}, method="PUT")
        cfg7 = json.loads(http(C + "/admin/config")[1])
        ri = rinfo(*http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {}})[::-1])
        check("UI-Client geloescht samt Einstellungen; anonyme Anfrage wieder anonym", st == 200 and "pi-voice" not in cfg7["clients"]
              and not any("pi-voice" in k for k in cfg7["overrides_settings"]) and ri.get("client") is None, f"{st} {ri.get('client')} {[k for k in cfg7['overrides_settings'] if 'pi-voice' in k]}")
        st, raw = http(C + "/admin/clients/gibtsnicht/token", {}, method="POST")
        check("Secret fuer unbekannten Client -> 404", st == 404)
        http(C + "/admin/config", {"settings": {"router.client_auth.mode": "enforce"}}, method="PUT")
        st, raw = http(C + "/admin/try", {"model": "standard:latest", "prompt": "hi"}, timeout=120)
        tr = json.loads(raw) if st == 200 else {}
        check("Ausprobieren im enforce-Modus: Router weist sich selbst als skirnir-ui aus (Start-Zufall, nur localhost)", st == 200 and tr.get("status") == 200
              and (tr.get("decision") or {}).get("client") == "skirnir-ui", f"{st} {tr.get('status')} {tr.get('answer')} {(tr.get('decision') or {}).get('client')}")
        check("Ausprobieren liefert Wartezeit (queued_ms) und Modellzeit (backend_ms) getrennt", isinstance(tr.get("backend_ms"), int) and "queued_ms" in tr, str({k: tr.get(k) for k in ("seconds", "queued_ms", "backend_ms", "load_ms")}))
        http(C + "/admin/config", {"settings": {"router.client_auth.mode": None}}, method="PUT")
        cv = json.loads(http(C + "/admin/config")[1])["clients"]
        check("Clients-Sicht zeigt skirnir-ui als interne Identitaet ohne Hash", cv.get("skirnir-ui", {}).get("source") == "intern" and not cv["skirnir-ui"]["token_prefix"])
        st, raw = http(C + "/admin/loadtest", {"model": "standard:latest", "n": 8, "concurrency": 4, "probe_interactive": True, "prompt": "hi"}, timeout=180)
        lt = json.loads(raw) if st == 200 else {}
        check("Lasttest: n Anfragen parallel durch den Router, Kennzahlen und interactive-Sonden", st == 200 and lt.get("normal", {}).get("ok") == 8 and lt.get("throughput", 0) > 0
              and lt["normal"]["wall_p50"] is not None and lt["normal"]["queue_max"] is not None and lt.get("interactive") is not None
              and "big" in lt["normal"]["nodes"], f"{st} {raw.decode()[:200]}")
        check("Lasttest im Entscheidungsprotokoll", any(d.get("event") == "loadtest" for d in state()["decisions"]))

        # --- Decision Engine (2026-09-16): Auto-role via chain Fake-Jevlike -> Rules ---
        dh = lambda tok: {"Authorization": "Bearer " + tok}   # noqa: E731 - hdr() gibt es erst im Client-Auth-Block
        tags_ = json.loads(http(R + "/api/tags", headers=dh("tester-token"))[1])["models"]
        check("Decision: auto:latest steht in /api/tags", any(m["name"] == "auto:latest" for m in tags_))
        st, raw = http(R + "/api/chat", {"model": "auto:latest", "stream": False, "routing": {}, "messages": [{"role": "user", "content": "Wie warm ist es im Wohnzimmer? [[assist]]"}]}, headers=dh("tester-token"), timeout=120)
        j = json.loads(raw) if st == 200 else {}
        dec = (j.get("routing") or {}).get("decision") or {}
        check("Decision: Jevlike sicher (0,85) -> Rolle assist, Entscheidung in routing.decision", st == 200 and j.get("routing", {}).get("role") == "assist"
              and dec.get("engine") == "jevlike" and dec.get("selected") == "assist" and not dec.get("fallback") and dec.get("model_probability", 0) > 0.8
              and dec.get("calibrated_confidence") is None, f"{st} {dec}")
        st, raw = http(R + "/api/chat", {"model": "auto:latest", "stream": False, "routing": {}, "messages": [{"role": "user", "content": "UNSICHER: schalte das licht im wohnzimmer an"}]}, headers=dh("tester-token"), timeout=120)
        dec = (json.loads(raw).get("routing") or {}).get("decision") or {} if st == 200 else {}
        check("Decision: Jevlike unsicher (0,46/0,43) -> Fallback Regeln waehlt assist", st == 200 and dec.get("engine") == "rules" and dec.get("selected") == "assist"
              and any(f.startswith("jevlike:") for f in dec.get("fallback", [])), f"{st} {dec}")
        st, raw = http(R + "/api/chat", {"model": "auto:latest", "stream": False, "routing": {}, "messages": [{"role": "user", "content": "AUSFALL: analysiere ausfuehrlich die vor- und nachteile"}]}, headers=dh("tester-token"), timeout=120)
        dec = (json.loads(raw).get("routing") or {}).get("decision") or {} if st == 200 else {}
        check("Decision: Jevlike ausgefallen (500) -> Regeln entscheiden, Ausfall protokolliert", st == 200 and dec.get("engine") == "rules"
              and "jevlike: ausgefallen" in dec.get("fallback", []) and dec.get("selected") == "gross", f"{st} {dec}")
        st, raw = http(C + "/admin/decide", {"context": "schalte das licht im wohnzimmer an", "engine": "rules"})
        dr = json.loads(raw) if st == 200 else {}
        check("/admin/decide: Regel-Engine direkt, volle Verteilung und Unsicherheitsmasse", st == 200 and dr.get("selected") == "assist"
              and abs(sum(dr.get("probabilities", {}).values()) - 1) < 1e-6 and "margin" in dr.get("uncertainty", {}), f"{st} {raw.decode()[:160]}")
        st, raw = http(C + "/admin/decide", {"body": {"model": "auto:latest", "messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "[[gross]] plane"}], "tools": [{"type": "function"}]}})
        dr = json.loads(raw) if st == 200 else {}
        check("/admin/decide mit Ollama-Body: Kontext = letzte Benutzernachricht + Hinweise", st == 200 and dr.get("selected") == "gross" and "[tools=1]" in dr.get("context", ""), f"{st} {raw.decode()[:160]}")
        st, raw = http(C + "/admin/decision")
        ds = json.loads(raw) if st == 200 else {}
        check("/admin/decision: Status mit Kette, Policy und Engine-Gesundheit", st == 200 and ds.get("chain") == ["jevlike", "rules"] and ds["engines"]["jevlike"]["ok"] is True
              and "rules" in ds["engines"], f"{st} {raw.decode()[:160]}")
        st, raw = http(C + "/admin/decide", {"context": "Schreib eine Python-Funktion, die eine CSV-Datei einliest und die Zeilen zaehlt.", "engine": "tfidf"})
        dt = json.loads(raw) if st == 200 else {}
        check("/admin/decide: tfidf (Zeichen-n-Gramme + Softmax-Regression, im Prozess) waehlt code, Latenz unter 5 ms",
              st == 200 and dt.get("selected") == "code" and dt.get("latency_ms", 99) < 5 and dt.get("model", "").startswith("skirnir-tfidf"), f"{st} {raw.decode()[:160]}")
        st, raw = http(C + "/admin/decide", {"context": "[[gross]] plane", "engine": "embed"})
        de_ = json.loads(raw) if st == 200 else {}
        check("/admin/decide: embed (Stufe 2, gleiches HTTP-Protokoll wie jevlike) antwortet unter eigenem Namen", st == 200 and de_.get("engine") == "embed" and de_.get("selected") == "gross", f"{st} {raw.decode()[:120]}")
        st, raw = http(C + "/admin/decide", {"context": "irgendwas", "engine": "local_llm"})
        check("/admin/decide: local_llm ueber den eigenen Router antwortet (oder faellt sauber auf default)", st == 200 and json.loads(raw).get("engine") in ("local_llm", "default"), f"{st} {raw.decode()[:120]}")
        http(R + "/api/chat", {"model": "assist:latest", "stream": False, "messages": [{"role": "user", "content": "Mail an max@example.org: Licht in 192.168.1.20 an"}]}, headers=dh("tester-token"), timeout=120)
        time.sleep(0.3)
        rows = [json.loads(l) for l in open(os.path.join(HERE, "decisions.jsonl"), encoding="utf-8") if l.strip()] if os.path.exists(os.path.join(HERE, "decisions.jsonl")) else []
        eng = [r for r in rows if r["label_source"] == "engine"]; cli = [r for r in rows if r["label_source"] == "client"]
        check("Decision-Capture: Engine-Pseudolabels und Client-Label getrennt, Kontext anonymisiert (E-Mail, IP), Gruppe gesetzt",
              len(eng) >= 3 and len(cli) >= 1 and cli[-1]["options"][cli[-1]["label"]] == "assist" and "<email>" in cli[-1]["context"] and "<ip>" in cli[-1]["context"]
              and all(r.get("group") for r in rows), f"engine={len(eng)} client={len(cli)} {cli[-1]['context'][:80] if cli else ''}")
        mtxt = http(C + "/metrics")[1].decode()
        check("/metrics: Decision-Zaehler, Latenz-Histogramm und Fallback-Zaehler", "skirnir_decision_total{" in mtxt and "skirnir_decision_latency_seconds_bucket" in mtxt
              and 'skirnir_decision_fallback_total{from="jevlike"' in mtxt, mtxt.count("skirnir_decision"))
        st, raw = http(C + "/admin/config", {"settings": {"decision_engine.policy.min_margin": 0.9}}, method="PUT")
        st2, raw2 = http(R + "/api/chat", {"model": "auto:latest", "stream": False, "routing": {}, "messages": [{"role": "user", "content": "[[gross]] plane die Migration"}]}, headers=dh("tester-token"), timeout=120)
        dec = (json.loads(raw2).get("routing") or {}).get("decision") or {} if st2 == 200 else {}
        check("Policy per UI-Einstellung: Abstand 0,9 verlangt -> Jevlike (0,85/0,05) gilt als unsicher, Regeln uebernehmen", st == 200 and st2 == 200
              and dec.get("engine") == "rules" and any("margin" in f for f in dec.get("fallback", [])), f"{st} {st2} {dec}")
        http(C + "/admin/config", {"settings": {"decision_engine.policy.min_margin": None}}, method="PUT")

        # Unload tail: /api/ps reports nothing immediately, nvidia-smi still shows memory. Without
        # vram_settle_s, the freed memory appears as foreign VRAM (26600 MiB busy - 0 Ollama
        # - 7 GiB baseline = 19 GiB "foreign") -> node falsely busy and prewarm blocked.
        vor = state()["nodes"]["big"]
        http("https://127.0.0.1:21001/api/generate", {"model": "qwen3.6:35b-a3b", "keep_alive": 0},
             headers={"X-Router-Token": "testtoken"}, auth=False)
        n = vor
        for _ in range(8):   # until the poll sees the empty /api/ps; Heartbeat keeps reporting high busy VRAM
            hb("big", 2, 26600, 6000); time.sleep(0.5)
            n = state()["nodes"]["big"]
            if not n["loaded"]:
                break
        check("Unload-Nachlauf: freiwerdendes VRAM gilt nicht als fremd", vor["loaded"] and not n["loaded"]
              and n["foreign_vram_gib"] == 0 and n["state"] == "free",
              f"geladen {list(vor['loaded'])} -> {list(n['loaded'])}, fremd {n['foreign_vram_gib']} GiB, {n['state']}")

        # Load tail (reverse direction): while Ollama loads, nvidia-smi sees the model, /api/ps does
        # not. Without announce_load, the loading model counts as foreign VRAM - and a cold start taking
        # longer than foreign_sustain_s makes the node busy mid-load; the safety net then unloads
        # exactly that model just loaded. `slow_load` keeps the Fake-Ollama in this state for 3 s.
        # Let the unload tail settle first (vram_settle_s = 8 s), otherwise THAT covers the window
        # and the test would pass even without announce_load (exactly what happened before this loop existed).
        for _ in range(7):
            hb("big", 3, 8000, 24600); time.sleep(1.5)   # low usage: no foreign VRAM, node stays free

        # Baseline learning rule (nothing loaded, so router is learning): median of hourly averages, NOT
        # the minimum. A single very quiet moment - monitor off - must not permanently
        # lower the baseline consumption; that is exactly why gpu-desktop was falsely busy.
        def gelernt():
            r = next(n for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")
            return (r.get("learned") or {}).get("baseline_gib")
        hb("big", 3, 2000, 30000)      # Downward outlier: 1.95 GiB busy
        for _ in range(6):
            hb("big", 3, 9000, 23000); time.sleep(0.3)   # Normalbetrieb: 8.79 GiB
        b1 = gelernt()
        check("Baseline lernt den Normalbetrieb, nicht den ruhigsten Moment", b1 is not None and b1 > 4.0,
              f"gelernt {b1} GiB (Ausreisser 1.95, Normalfall 8.79 - das alte Minimum haette 1.95 geliefert)")
        # Game hours do not belong here: under high load, no sampling occurs
        for _ in range(3):
            hb("big", 80, 30000, 2000); time.sleep(0.2)   # "Game": 29.3 GiB busy, GPU hot
        b2 = gelernt()
        check("bei heisser GPU wird nicht gelernt (ein Spiel faelscht die Baseline nicht)", b2 == b1,
              f"{b1} -> {b2} GiB")
        hb("big", 3, 8000, 24600)   # quiet again for the next test
        res = {}

        def lade():
            res["st"], _ = http(R + "/api/chat", {"model": "standard:latest", "stream": False, "slow_load": 3,
                                                  "messages": [{"role": "user", "content": "hi"}],
                                                  "options": {"num_ctx": 16384}}, timeout=30)
        th = threading.Thread(target=lade, daemon=True)
        th.start()
        time.sleep(1.0)             # mitten im Ladevorgang
        hb("big", 2, 26600, 6000)   # nvidia-smi sees the model already: 26000 MiB busy
        n = state()["nodes"]["big"]
        # since 2026-09-11, during loading, at least the foreign VRAM from BEFORE loading continues to count (here 0.8 GiB from
        # 7.8 busy - 7 baseline); only the loading model itself must not appear as foreign
        check("Lade-Nachlauf: ladendes Modell gilt nicht als fremdes VRAM", not n["loaded"]
              and n["foreign_vram_gib"] <= 0.9 and n["state"] == "free",
              f"geladen {list(n['loaded'])}, fremd {n['foreign_vram_gib']} GiB, {n['state']}")
        th.join(timeout=25)
        check("... und die Anfrage laeuft normal durch", res.get("st") == 200, str(res.get("st")))

        # Busy thresholds per node (Policy): big with busy_foreign_gib 0.5 -> 1 GiB foreign makes busy, although global is 3.0
        fpb = next(n["fp"] for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")
        st, raw = http(C + f"/admin/nodes/{fpb}/policy", {"busy_foreign_gib": 0.5, "busy_gpu_util_pct": 90})
        for _ in range(12):   # Policy change sends a config package to the agent, tunnel comes up briefly
            if state()["nodes"].get("big", {}).get("state") not in (None, "offline"):
                break
            time.sleep(0.5)
        sb = state()["nodes"]["big"]
        base_b = (sb.get("baseline_gib") or 0) + sum(sb["loaded"].values())
        for _ in range(4):
            hb("big", 5, int((base_b + 1.0) * 1024), int(32607 - (base_b + 1.0) * 1024)); time.sleep(0.7)
        check("Busy-Schwelle je Knoten: 1 GiB fremd macht big busy (Policy 0.5 GiB, global 3.0)", st == 200 and state()["nodes"]["big"]["state"] == "busy"
              and state()["nodes"]["big"]["busy_reason"] == "foreign_vram", f"{st} {raw.decode()[:80]} {state()['nodes']['big']['state']} foreign={state()['nodes']['big'].get('foreign_vram_gib')}")
        st, raw = http(C + f"/admin/nodes/{fpb}/policy", {"busy_foreign_gib": 200})
        check("Busy-Schwelle ausserhalb des Bereichs -> 400", st == 400, raw.decode()[:80])
        st, raw = http(C + f"/admin/nodes/{fpb}/policy", {"mac": "nicht-hex"})
        check("Policy: ungueltige MAC -> 400 (frueher 500 beim Weckversuch)", st == 400 and "mac" in raw.decode(), raw.decode()[:80])
        st, raw = http(C + f"/admin/nodes/{fpb}/policy", {"mac": "AA-BB-CC-DD-EE-01", "busy_foreign_gib": None, "busy_gpu_util_pct": None})
        check("Policy: MAC wird normalisiert", st == 200 and json.loads(raw)["policy"].get("mac") == "aa:bb:cc:dd:ee:01", raw.decode()[:80])
        http(C + f"/admin/nodes/{fpb}/policy", {"mac": None})
        for _ in range(12):
            if state()["nodes"].get("big", {}).get("state") not in (None, "offline"):
                break
            time.sleep(0.5)
        sb = state()["nodes"]["big"]; base_b = (sb.get("baseline_gib") or 0) + sum(sb["loaded"].values())
        for _ in range(5):
            hb("big", 5, int(base_b * 1024), int(32607 - base_b * 1024)); time.sleep(0.7)
        check("Policy zurueck auf global: big wieder free", state()["nodes"]["big"]["state"] == "free", state()["nodes"]["big"]["state"])

        # Router 0.7.0 / Agent 0.17.0: Game and memory as fact. Trigger 2026-10-04: Diablo IV next to qwen3.8@196k -> RAM 94 %,
        # Node stayed free (little foreign VRAM, no load). A reported game makes busy, independent of VRAM and load.
        viel = {"ram_available_gib": 30.0, "commit_free_gib": 40.0, "ram_total_gib": 63.4, "commit_limit_gib": 132.0}
        spiel = {"supported": True, "running": True, "name": "Diablo IV.exe", "via": "pfad"}
        ruhig = (int(base_b * 1024), int(32607 - base_b * 1024))
        for _ in range(4):   # game_sustain_s 1 im Test
            hb("big", 5, *ruhig, game=spiel, memory=viel); time.sleep(0.6)
        nb = state()["nodes"]["big"]
        check("Agent meldet Spiel -> big busy mit Grund 'game', obwohl kein fremdes VRAM und kaum Last",
              nb["state"] == "busy" and nb["busy_reason"] == "game" and (nb.get("game") or {}).get("name") == "Diablo IV.exe",
              f"{nb['state']} {nb['busy_reason']} {nb.get('game')} fremd {nb.get('foreign_vram_gib')}")
        check("... ohne gemeldeten GPU-Bedarf (Agent < 0.19.0) wie bis 0.8.6: schwer, Grund 'nicht gemeldet'",
              nb["game_heavy"] is True and nb["game_heavy_why"] == "GPU use not reported", f"{nb['game_heavy']} {nb['game_heavy_why']}")
        st, raw = http(R + "/v1/skirnir/availability/gross:latest")
        av = json.loads(raw) if st == 200 else {}
        bl = [b for b in av.get("blockers", []) if b.get("node") == "big"]
        check("... Verfuegbarkeit: gpu_busy mit busy_reason game und Spielname, Klartext nennt das Spiel", av.get("code") == "gpu_busy"
              and bl and bl[0].get("busy_reason") == "game" and bl[0].get("game") == "Diablo IV.exe" and "game" in av.get("detail", ""),
              f"{st} {str(av)[:240]}")
        hb("big", 5, *ruhig, game={"supported": True, "running": False}, memory=viel); time.sleep(1.2)
        hb("big", 5, *ruhig, game={"supported": True, "running": False}, memory=viel)
        check("... Spiel beendet: busy bleibt waehrend game_quiet_s (Neustart ueberbruecken)", state()["nodes"]["big"]["state"] == "busy",
              state()["nodes"]["big"]["state"])
        for _ in range(8):
            hb("big", 5, *ruhig, game={"supported": True, "running": False}, memory=viel); time.sleep(0.6)
            if state()["nodes"]["big"]["state"] == "free":
                break
        check("... danach free", state()["nodes"]["big"]["state"] == "free", state()["nodes"]["big"]["state"])

        # Router 0.8.7 / Agent 0.19.0: agent reports the GPU demand of the game. A light game (measured 735 MiB own,
        # 32 MiB shared, 2 %) no longer locks the card; one that needs the GPU does - even if only in the shared
        # Speicher ausweicht (Diablo IV neben qwen3.8: kaum VRAM, kaum Last).
        leicht = dict(spiel, name="Small Game.exe", procs=1, gpu_dedicated_mib=735, gpu_shared_mib=32, gpu_pct=1.8)
        for _ in range(4):   # game_sustain_s 1 im Test
            hb("big", 5, *ruhig, game=leicht, memory=viel); time.sleep(0.6)
        nb = state()["nodes"]["big"]
        check("Leichtes Spiel (735+32 MiB, 1,8 %) -> big bleibt free, game_heavy false mit Grund 'light'",
              nb["state"] == "free" and nb["game_heavy"] is False and nb["game_heavy_why"].startswith("light")
              and (nb.get("game") or {}).get("gpu_dedicated_mib") == 735, f"{nb['state']} {nb['game_heavy']} {nb['game_heavy_why']} {nb.get('game')}")
        st, raw = http(R + "/v1/skirnir/availability/gross:latest")
        av = json.loads(raw) if st == 200 else {}
        check("... Verfuegbarkeit ohne gpu_busy-Blocker fuer big", not [b for b in av.get("blockers", []) if b.get("node") == "big"
              and b.get("busy_reason") == "game"], f"{st} {str(av)[:200]}")
        verdraengt = dict(leicht, gpu_dedicated_mib=300, gpu_shared_mib=2100, gpu_pct=4.0)
        t_heavy = time.time()
        for _ in range(3):
            hb("big", 5, *ruhig, game=verdraengt, memory=viel); time.sleep(0.6)
        nb = state()["nodes"]["big"]
        ev = [d for d in state()["decisions"] if d.get("event") == "game_heavy" and d.get("t", 0) >= t_heavy]
        check("Spiel weicht in geteilten Speicher aus (300+2100 MiB, 4 %) -> busy 'game', Grund Grafikspeicher, Journal game_heavy",
              nb["state"] == "busy" and nb["busy_reason"] == "game" and nb["game_heavy"] is True and "graphics memory" in nb["game_heavy_why"]
              and ev, f"{nb['state']} {nb['busy_reason']} {nb['game_heavy_why']} {ev}")
        for _ in range(3):
            hb("big", 5, *ruhig, game=leicht, memory=viel); time.sleep(0.6)
        check("... wieder leicht (Menue): bleibt busy bis Spielende (kein Flattern)", state()["nodes"]["big"]["state"] == "busy",
              state()["nodes"]["big"]["state"])
        for _ in range(10):
            hb("big", 5, *ruhig, game={"supported": True, "running": False}, memory=viel); time.sleep(0.6)
            if state()["nodes"]["big"]["state"] == "free":
                break
        nb = state()["nodes"]["big"]
        check("... Spiel beendet -> free, game_heavy zurueckgesetzt", nb["state"] == "free" and nb["game_heavy"] is False,
              f"{nb['state']} {nb['game_heavy']}")
        rechnet = dict(leicht, gpu_dedicated_mib=900, gpu_shared_mib=0, gpu_pct=35.0)
        for _ in range(3):
            hb("big", 5, *ruhig, game=rechnet, memory=viel); time.sleep(0.6)
        nb = state()["nodes"]["big"]
        check("Spiel mit wenig Speicher, aber 35 % Engine -> busy, Grund GPU-Engine", nb["state"] == "busy" and "GPU engine" in nb["game_heavy_why"],
              f"{nb['state']} {nb['game_heavy_why']}")
        for _ in range(10):
            hb("big", 5, *ruhig, game={"supported": True, "running": False}, memory=viel); time.sleep(0.6)
            if state()["nodes"]["big"]["state"] == "free":
                break
        check("... danach free", state()["nodes"]["big"]["state"] == "free", state()["nodes"]["big"]["state"])
        eng = dict(viel, ram_available_gib=3.0, commit_free_gib=2.0)
        for _ in range(4):   # polls 2
            hb("big", 5, *ruhig, game=leicht, memory=eng); time.sleep(0.6)
        nb = state()["nodes"]["big"]
        check("Leichtes Spiel bei Speicherdruck (Diablo-Fall: RAM 94 %) -> busy, Grund Speicherdruck",
              nb["state"] == "busy" and nb["game_heavy_why"] == "memory pressure", f"{nb['state']} {nb['game_heavy_why']} {nb['memory_pressure']}")
        for _ in range(10):
            hb("big", 5, *ruhig, game={"supported": True, "running": False}, memory=viel); time.sleep(0.6)
            if state()["nodes"]["big"]["state"] == "free" and not state()["nodes"]["big"]["memory_pressure"]:
                break
        check("... Spiel weg, Speicher frei -> free", state()["nodes"]["big"]["state"] == "free", state()["nodes"]["big"]["state"])
        # Memory pressure: no cold load, no prewarm - the loaded one keeps responding, nothing is evicted
        knapp = dict(viel, ram_available_gib=2.5, commit_free_gib=1.8)
        nb = state()["nodes"]["big"]; sm = state()["nodes"].get("small", {})
        kalt = next((m for m in nb["models"] if m not in nb["loaded"] and m not in (sm.get("models") or [])), None)
        geladen_vorher = dict(nb["loaded"])
        t_mp2 = time.time()
        for _ in range(3):   # polls 2
            hb("big", 5, *ruhig, game={"supported": True, "running": False}, memory=knapp); time.sleep(0.5)
        nb = state()["nodes"]["big"]
        ev = [d for d in state()["decisions"] if d.get("event") == "memory_pressure" and d.get("t", 0) >= t_mp2]
        check("Speicherdruck (RAM 2,5 / Commit 1,8 GiB, 2 Heartbeats) -> memory_pressure, Knoten bleibt free, nichts entladen",
              nb["memory_pressure"] is True and nb["state"] == "free" and ev and ev[0].get("on") is True
              and set(nb["loaded"]) == set(geladen_vorher), f"{nb['memory_pressure']} {nb['state']} {ev} {nb['loaded']} vorher {geladen_vorher}")
        if kalt:
            st, raw = http(R + f"/v1/skirnir/availability/{kalt}")
            av = json.loads(raw) if st == 200 else {}
            check(f"... kaltes Modell ({kalt}) wird nicht geladen: available false, code memory_pressure",
                  av.get("available") is False and av.get("code") == "memory_pressure", f"{st} {str(av)[:200]}")
        for _ in range(2):
            hb("big", 5, *ruhig, game={"supported": True, "running": False}, memory=viel); time.sleep(0.4)
        check("... Speicher wieder frei -> memory_pressure vorbei", state()["nodes"]["big"]["memory_pressure"] is False,
              str(state()["nodes"]["big"].get("memory")))

        # Tunnel gone (agent dies) -> big immediately offline; agent back -> big free again
        agent.terminate(); agent.wait()
        for _ in range(10):
            if state()["nodes"]["big"]["state"] == "offline": break
            time.sleep(0.3)
        check("Tunnel getrennt -> big sofort offline", state()["nodes"]["big"]["state"] == "offline", state()["nodes"]["big"]["state"])
        agent = subprocess.Popen([PY, os.path.join(HERE, "fake_agent.py"), "wss://127.0.0.1:21435", "big", "https://127.0.0.1:21001", os.path.join(HERE, "fake-agent-big.key"), "testtoken"],
                                 stdout=logs["agent-big"], stderr=subprocess.STDOUT, cwd=HERE)
        procs.append(agent)
        for _ in range(20):
            if state()["nodes"]["big"]["state"] == "free": break
            time.sleep(0.3)
        check("Tunnel wieder da -> big wieder free", state()["nodes"]["big"]["state"] == "free", state()["nodes"]["big"]["state"])
        check("... und meldet den Ollama-Zustand wieder selbst", state()["nodes"]["big"].get("ollama_source") == "push", str(state()["nodes"]["big"].get("ollama_source")))
        check("Tunnel-Ereignisse protokolliert", [d["state"] for d in state()["decisions"] if d["event"] == "tunnel"][-2:] == ["down", "up"])
        st, txt = chat("standard:latest", ctx=16384)
        check("Anfrage durch den neuen Tunnel", st == 200, f"{st} {txt[:60]}")
        # Node big dies (Ollama gone, tunnel stays) -> offline -> WOL attempt -> 503 after wait_up_s
        procs[0].terminate(); procs[0].wait()
        for _ in range(20):   # 2 misses x (1 s poll + connect error), Windows needs ~2 s per refused connect
            if state()["nodes"]["big"]["state"] == "offline": break
            time.sleep(0.5)
        check("big offline erkannt", state()["nodes"]["big"]["state"] == "offline")
        rlog.seek(0)
        check("... weil der Agent 'Ollama antwortet nicht' meldet (nicht aus fehlgeschlagenen Abfragen erschlossen)",
              "offline (Ollama not answering according to the agent" in rlog.read())
        t0 = time.time()
        st, txt = chat("gross:latest")
        dt = time.time() - t0
        ev = [d["event"] for d in state()["decisions"][-5:]]
        check("gross ohne Knoten: WOL versucht, dann 503", st == 503 and "wol" in ev and 3.5 <= dt <= 8, f"{st} {dt:.1f}s {ev}")
        st, txt = chat("standard:latest", ctx=4096)
        d = route()
        check("standard 4096 -> small (granite tier2, 4.95+0.65+0.8=6.4 <= 7)", st == 200 and d["node"] == "small", str(d))

        # Cooldown: second big call must NOT wait 4 s again
        t0 = time.time(); st, txt = chat("gross:latest"); dt = time.time() - t0
        check("WOL-Cooldown: sofort 503", st == 503 and dt < 1.5, f"{dt:.1f}s")

        # specific models: present -> routed, unknown -> 404 like Ollama
        st, txt = chat("granite4.2:8b", ctx=4096)
        d = route()
        check("konkretes Modell wird 1:1 geroutet", st == 200 and d["model"] == "granite4.2:8b" and json.loads(txt.splitlines()[0])["model"] == "granite4.2:8b", str(d))
        st, txt = chat("gibtsnicht:7b")
        check("unbekanntes konkretes Modell -> 404", st == 404 and "not found" in txt, f"{st} {txt[:60]}")
        # Basic Auth on the control port
        st, raw = http(C + "/", auth=False)
        check("UI ohne Login -> 401 (über TLS)", st == 401)
        try:
            urllib.request.urlopen("http://127.0.0.1:21435/", timeout=5); plain = "antwortet"
        except Exception as ex:  # noqa: BLE001
            plain = type(ex).__name__
        check("Klartext-HTTP auf dem TLS-Port wird abgewiesen", plain != "antwortet", plain)
        st, raw = http(C + "/admin/state", headers={"Authorization": "Basic " + base64.b64encode(b"tester:falsch").decode()}, auth=False)
        check("falsches Passwort -> 401", st == 401)
        sts = [http(C + "/admin/state", headers={"Authorization": "Basic " + base64.b64encode(f"tester:falsch{i}".encode()).decode()}, auth=False)[0] for i in range(5)]
        check("Login-Bremse: nach 5 Fehlversuchen einer Adresse in 60 s -> 429 ohne Hashing (Retry-After)", sts[:4] == [401] * 4 and sts[4] == 429 and LAST_HEADERS.get("Retry-After") == "60", str(sts))
        st, raw = http(C + "/admin/state")
        check("... geprueftes Passwort bleibt aus dem Cache erlaubt", st == 200, str(st))
        st, raw = http(C + "/admin/config", {"roles": {}}, method="PUT", auth=False)
        check("PUT ohne Login -> 401", st == 401)
        # UI + Konfig-API
        st, raw = http(C + "/")
        check("UI wird ausgeliefert", st == 200 and b"Skirnir" in raw)
        st, raw = http(C + "/admin/config")
        cfg = json.loads(raw)
        check("GET /admin/config liefert Rollen", st == 200 and "standard" in cfg["roles"] and cfg["roles"]["standard"]["tiers"][0]["model"] == "qwen3.6:35b-a3b")
        cfg["roles"]["klein"] = {"latency_first": False, "tiers": [{"model": "granite4.2:8b", "num_ctx": 4096, "busy_ok": True}]}
        st, raw = http(C + "/admin/config", {"roles": cfg["roles"], "expose_concrete_models": False}, method="PUT")
        check("PUT /admin/config akzeptiert neue Rolle", st == 200, raw.decode()[:120])
        st, raw = http(R + "/api/tags"); names = [m["name"] for m in json.loads(raw)["models"]]
        check("neue Rolle sofort in /api/tags, konkrete Modelle ausgeblendet", "klein:latest" in names and "granite4.2:8b" not in names, str(names))
        check("roles.yaml-Override (UI) behaelt priority/canary/shadow aus config.yaml", state()["roles"]["assist:latest"]["priority"] == "interactive"
              and (json.loads(http(C + "/admin/config")[1])["roles"]["kanarie"].get("canary") or {}).get("percent") == 100,
              str(state()["roles"]["assist:latest"]))
        st, txt = chat("klein")
        d = next((x for x in reversed(state()["decisions"]) if x["event"] == "route"), {})
        check("neue Rolle routet (granite auf small)", st == 200 and d["model"] == "granite4.2:8b", str(d))
        st, raw = http(C + "/admin/config", {"roles": {"kaputt": {"tiers": [{"model": "gibtsnicht:1b", "num_ctx": 8}]}}}, method="PUT")
        check("PUT mit unbekanntem Modell -> 400", st == 400, raw.decode()[:100])

        # OpenAI-compatible (/v1): Node-RED-MCP speaks chat/completions without streaming, with tools/response_format
        st, raw = http(R + "/v1/models", headers={"Authorization": "Bearer egal"})
        oam = json.loads(raw)
        ids = [m["id"] for m in oam.get("data", [])]
        check("/v1/models: object list, Rollen drin, konkrete ausgeblendet", st == 200 and oam["object"] == "list" and "klein:latest" in ids
              and "standard:latest" in ids and "granite4.2:8b" not in ids and oam["data"][0]["object"] == "model", str(ids))
        st, raw = http(R + "/v1/models/klein")
        check("/v1/models/<rolle ohne tag> -> 200", st == 200 and json.loads(raw)["id"] == "klein:latest", raw.decode()[:80])
        st, raw = http(R + "/v1/models/gibtsnicht")
        check("/v1/models/unbekannt -> 404 im OpenAI-Fehlerformat", st == 404 and "message" in json.loads(raw)["error"], raw.decode()[:80])
        oa_body = {"model": "klein", "messages": [{"role": "system", "content": "Du bist knapp."},
                                                  {"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                   "temperature": 0.3, "max_tokens": 64, "response_format": {"type": "json_object"}}
        t_oa = time.time()   # no chat() -> time for route() to note itself
        st, raw = http(R + "/v1/chat/completions", oa_body, headers={"Authorization": "Bearer egal"})
        oa = json.loads(raw)
        check("/v1/chat/completions non-stream: chat.completion mit Rollenname + usage", st == 200 and oa["object"] == "chat.completion"
              and oa["model"] == "klein:latest" and oa["choices"][0]["message"]["content"] == "Hallo von small"
              and oa["choices"][0]["finish_reason"] == "stop" and oa["usage"] == {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}
              and oa["id"].startswith("chatcmpl-"), raw.decode()[:200])
        logs["small"].seek(0); sent = last_route(logs["small"].read())[-1]
        check("... nativ uebersetzt: num_ctx 4096 (Tier), keep_alive -1, format json, num_predict 64, temperature, think false",
              sent["num_ctx"] == 4096 and sent["keep_alive"] == -1 and sent["stream"] is False and sent["format"] == "json"
              and sent["options"].get("num_predict") == 64 and sent["options"].get("temperature") == 0.3 and sent["think"] is False, str(sent))
        d = route(t_oa)
        check("Entscheidung protokolliert mit via=openai", d.get("event") == "route" and d.get("via") == "openai" and d.get("model") == "granite4.2:8b", str(d))
        tools = [{"type": "function", "function": {"name": "get_time", "description": "Uhrzeit", "parameters": {"type": "object", "properties": {"tz": {"type": "string"}}}}}]
        st, raw = http(R + "/v1/chat/completions", {"model": "klein:latest", "messages": [{"role": "user", "content": "wie spaet?"}], "tools": tools, "think": True})
        oa = json.loads(raw)
        tc = oa["choices"][0]["message"].get("tool_calls") or [{}]
        check("Tool-Call: arguments als JSON-String, id call_*, finish_reason tool_calls", st == 200 and oa["choices"][0]["finish_reason"] == "tool_calls"
              and tc[0].get("type") == "function" and tc[0]["function"]["name"] == "get_time" and json.loads(tc[0]["function"]["arguments"]) == {"tz": "CET"}
              and tc[0].get("id", "").startswith("call-"), raw.decode()[:220])
        logs["small"].seek(0); sent = last_route(logs["small"].read())[-1]
        check("... tools durchgereicht, think true (explizit)", sent["tools"] is True and sent["think"] is True, str(sent))
        # Tool result back: assistant.tool_calls (arguments string) + role tool -> native (arguments object)
        follow = {"model": "klein", "messages": [{"role": "user", "content": "wie spaet?"},
                                                 {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": "{\"tz\": \"CET\"}"}}]},
                                                 {"role": "tool", "tool_call_id": "call_1", "content": "12:00"}]}
        st, raw = http(R + "/v1/chat/completions", follow)
        check("Tool-Ergebnis-Runde wird akzeptiert", st == 200 and json.loads(raw)["choices"][0]["message"]["content"] == "Hallo von small", raw.decode()[:120])
        # Streaming als SSE
        st, raw = http(R + "/v1/chat/completions", {"model": "klein", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                                                    "stream_options": {"include_usage": True}})
        txt = raw.decode()
        events = [l[6:] for l in txt.split("\n") if l.startswith("data: ")]
        chunks = [json.loads(e) for e in events if e != "[DONE]"]
        content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
        finals = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
        usage = [c for c in chunks if not c["choices"] and c.get("usage")]
        check("/v1 stream: SSE-Chunks, Text vollstaendig, finish_reason stop, usage-Chunk, [DONE]", st == 200 and events[-1] == "[DONE]"
              and all(c["object"] == "chat.completion.chunk" and c["model"] == "klein:latest" for c in chunks) and content == "Hallo von small"
              and len(finals) == 1 and finals[0]["choices"][0]["finish_reason"] == "stop" and len(usage) == 1 and usage[0]["usage"]["total_tokens"] == 50,
              txt[:300].replace("\n", " "))
        st, raw = http(R + "/v1/chat/completions", {"model": "gibtsnicht", "messages": [{"role": "user", "content": "hi"}]})
        check("/v1 unbekanntes Modell -> 404 OpenAI-Fehler", st == 404 and "not found" in json.loads(raw)["error"]["message"], raw.decode()[:100])
        st, raw = http(R + "/v1/chat/completions", {"model": "klein"})
        check("/v1 ohne messages -> 400", st == 400, raw.decode()[:100])
        st, raw = http(R + "/v1/completions", {"model": "klein", "prompt": "Sag hallo", "max_tokens": 5})
        oa = json.loads(raw)
        check("/v1/completions (legacy) ueber /api/generate", st == 200 and oa["object"] == "text_completion" and oa["choices"][0]["text"] == "Hallo von small"
              and oa["model"] == "klein:latest", raw.decode()[:160])
        st, raw = http(R + "/v1/embeddings", {"model": "klein", "input": ["a", "b"]})
        oa = json.loads(raw)
        check("/v1/embeddings ueber /api/embed", st == 200 and oa["object"] == "list" and len(oa["data"]) == 2 and oa["data"][1]["index"] == 1
              and oa["data"][0]["embedding"] == [0.1, 0.2, 0.3] and oa["usage"]["prompt_tokens"] == 14 and oa["model"] == "klein:latest", raw.decode()[:160])
        st, raw = http(R + "/v1/embeddings", {"model": "klein", "input": "kein-embedding"})
        check("Knoten 501 (Modell kann keine Embeddings) -> Fehlertext an den Client, kein 503", st == 501 and "does not support embeddings" in json.loads(raw)["error"]["message"], f"{st} {raw.decode()[:120]}")
        st, raw = http(R + "/api/embed", {"model": "klein", "input": "kein-embedding"})
        check("... auch im Ollama-Format", st == 501 and "does not support embeddings" in json.loads(raw)["error"], f"{st} {raw.decode()[:120]}")
        # Anthropic Messages API (/v1/messages, Claude Code): Translation to /api/chat via role and scheduler
        def sse_events(raw):
            out = []
            for frame in raw.decode().split("\n\n"):
                lines = [l for l in frame.split("\n") if l]
                if len(lines) >= 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "):
                    out.append((lines[0][7:], json.loads(lines[1][6:])))
            return out

        an_hdr = {"anthropic-version": "2023-06-01", "anthropic-beta": "test-beta-2026-10-01"}
        an_body = {"model": "klein", "max_tokens": 64, "system": [{"type": "text", "text": "Du bist knapp.", "cache_control": {"type": "ephemeral"}}],
                   "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
        t_an = time.time()
        st, raw = http(R + "/v1/messages", an_body, headers=an_hdr)
        am = json.loads(raw)
        check("/v1/messages non-stream: message mit Rollenname, Text, end_turn, usage, id msg_<request_id>", st == 200
              and am.get("type") == "message" and am.get("role") == "assistant" and am.get("model") == "klein:latest"
              and am.get("content") == [{"type": "text", "text": "Hallo von small"}] and am.get("stop_reason") == "end_turn"
              and am.get("usage") == {"input_tokens": 20, "output_tokens": 30}
              and am.get("id") == "msg_" + str(LAST_HEADERS.get("X-Skirnir-Request-Id")), raw.decode()[:220])
        logs["small"].seek(0); sent = last_route(logs["small"].read())[-1]
        check("... nativ: /api/chat, num_ctx 4096 (Tier), num_predict 64, think false", sent["path"] == "/api/chat" and sent["num_ctx"] == 4096
              and sent["stream"] is False and sent["options"].get("num_predict") == 64 and sent["think"] is False, str(sent))
        d = route(t_an)
        check("... Entscheidung protokolliert mit via=anthropic", d.get("via") == "anthropic" and d.get("model") == "granite4.2:8b", str(d))
        an_tools = [{"name": "get_time", "description": "Uhrzeit", "input_schema": {"type": "object", "properties": {"tz": {"type": "string"}}}}]
        st, raw = http(R + "/v1/messages", {**an_body, "tools": an_tools, "messages": [{"role": "user", "content": "wie spaet?"}]}, headers=an_hdr)
        am = json.loads(raw)
        tu = [c for c in am.get("content", []) if c.get("type") == "tool_use"]
        check("/v1/messages Tool-Call: tool_use mit input-Objekt, stop_reason tool_use", st == 200 and am.get("stop_reason") == "tool_use"
              and len(tu) == 1 and tu[0]["name"] == "get_time" and tu[0]["input"] == {"tz": "CET"} and tu[0]["id"].startswith("toolu_"), raw.decode()[:220])
        follow = {**an_body, "tools": an_tools, "messages": [
            {"role": "user", "content": "wie spaet?"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": tu[0]["id"] if tu else "toolu_x", "name": "get_time", "input": {"tz": "CET"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu[0]["id"] if tu else "toolu_x", "content": "12:00"}]}]}
        st, raw = http(R + "/v1/messages", {**follow, "tools": []}, headers=an_hdr)
        check("... Tool-Ergebnis-Runde wird akzeptiert", st == 200 and json.loads(raw).get("content", [{}])[0].get("text") == "Hallo von small", raw.decode()[:160])
        st, raw = http(R + "/v1/messages", {**an_body, "stream": True}, headers=an_hdr)
        evs = sse_events(raw)
        names = [e for e, _ in evs]
        txt = "".join(d["delta"].get("text", "") for e, d in evs if e == "content_block_delta")
        check("/v1/messages stream: message_start, Textblock, message_delta mit Zaehlern, message_stop", st == 200
              and LAST_HEADERS.get("Content-Type", "").startswith("text/event-stream") and names[0] == "message_start"
              and names[-2:] == ["message_delta", "message_stop"] and txt == "Hallo von small"
              and evs[-2][1]["usage"] == {"input_tokens": 20, "output_tokens": 30} and evs[-2][1]["delta"]["stop_reason"] == "end_turn",
              f"{st} {names}")
        # Format like Claude Code 2.1.284 (Log 2026-10-01): ?beta=true, system role in history, thinking adaptive/omitted,
        # context_management/output_config - first acceptance failed on system role with 400
        cc_body = {**an_body, "stream": True, "thinking": {"type": "adaptive", "display": "omitted"}, "context_management": {},
                   "output_config": {}, "metadata": {"user_id": "x"}, "messages": [
                       {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "hi"}]},
                       {"role": "system", "content": [{"type": "text", "text": "Hinweis", "cache_control": {"type": "ephemeral"}}]}]}
        t_cc = time.time()
        st, raw = http(R + "/v1/messages?beta=true", cc_body, headers=an_hdr)
        names = [e for e, _ in sse_events(raw)]
        logs["small"].seek(0); sent = last_route(logs["small"].read())[-1]
        check("/v1/messages?beta=true in Claude-Code-Form (system im Verlauf, adaptive) -> 200-Stream, think true", st == 200
              and names[0] == "message_start" and names[-1] == "message_stop" and sent["think"] is True, f"{st} {names} {raw[:160]!r}")
        d = route(t_cc)
        check("... Entscheidungslog traegt think (Beleg fuer Messlaeufe)", d.get("think") is True, str(d))
        st_set, _ = http(C + "/admin/config", {"settings": {"router.anthropic.think": "off"}}, method="PUT")
        t_off = time.time()
        st, raw = http(R + "/v1/messages?beta=true", cc_body, headers=an_hdr)
        logs["small"].seek(0); sent = last_route(logs["small"].read())[-1]
        check("router.anthropic.think off (Einstellung, ohne Neustart): Claude-Code-adaptive -> think false", st_set == 200 and st == 200
              and sent["think"] is False and route(t_off).get("think") is False, f"{st_set} {st} {sent.get('think')}")
        st_set, _ = http(C + "/admin/config", {"settings": {"router.anthropic.think": "nein"}}, method="PUT")
        check("... ungueltiger Wert -> 400", st_set == 400, str(st_set))
        http(C + "/admin/config", {"settings": {"router.anthropic.think": None}}, method="PUT")
        st, raw = http(R + "/v1/messages", {**an_body, "stream": True, "tools": an_tools,
                                            "messages": [{"role": "user", "content": "wie spaet? [[stream_tool]]"}]}, headers=an_hdr)
        evs = sse_events(raw)
        starts = [d["content_block"]["type"] for e, d in evs if e == "content_block_start"]
        pj = [d["delta"]["partial_json"] for e, d in evs if e == "content_block_delta" and d["delta"]["type"] == "input_json_delta"]
        check("/v1/messages stream mit Tool: Text- und tool_use-Block, input_json_delta, stop_reason tool_use", st == 200
              and starts == ["text", "tool_use"] and [json.loads(p) for p in pj] == [{"tz": "CET"}]
              and evs[-2][1]["delta"]["stop_reason"] == "tool_use", f"{st} {[e for e, _ in evs]}")
        st, raw = http(R + "/v1/messages", {**an_body, "stream": True, "tools": an_tools,
                                            "messages": [{"role": "user", "content": "wie spaet? [[broken_tool_call]]"}]}, headers=an_hdr)
        evs = sse_events(raw)
        starts = [d["content_block"] for e, d in evs if e == "content_block_start"]
        check("/v1/messages stream: Tool-Call-Rettung greift (Text-Dialekt -> tool_use, kein Textblock)", st == 200
              and [b["type"] for b in starts] == ["tool_use"] and starts[0]["name"] == "get_time", f"{st} {[e for e, _ in evs]}")
        # 0.6.5: Catalog max_parallel - a second request to the same model waits in the router, not in Ollama's queue
        # (Ollama otherwise reloaded the runner for qwen3.8 as soon as the first finished; 2026-10-02 03:02 twice)
        # Catalog PUT merges (omitting does not delete): to reset, send the same entry without max_parallel
        granite = {"weights_gib": 4.95, "kv_gib_per_1k": 0.158}
        st_mp, raw_mp = http(C + "/admin/config", {"models": {"granite4.2:8b": {**granite, "max_parallel": 1}}}, method="PUT")
        t_mp = time.time()
        mp = {}

        def mp_los(tag, verzug):
            time.sleep(verzug)
            t = time.time()
            st_, raw_ = http(R + "/api/chat", {"model": "klein", "stream": False,
                                               "messages": [{"role": "user", "content": f"{tag} [[sleep_s=2]]"}]})
            mp[tag] = (st_, time.time() - t)
        ths = [threading.Thread(target=mp_los, args=("eins", 0)), threading.Thread(target=mp_los, args=("zwei", 0.4))]
        for t_ in ths:
            t_.start()
        for t_ in ths:
            t_.join(30)
        q_mp = [d for d in state()["decisions"] if d.get("event") == "queued" and d.get("t", 0) >= t_mp]
        check("max_parallel 1: zweite Anfrage wartet im Router (queued, reason max_parallel), beide 200, nacheinander statt zugleich",
              st_mp == 200 and mp.get("eins", (0,))[0] == 200 and mp.get("zwei", (0,))[0] == 200 and len(q_mp) == 1
              and q_mp[0].get("reason") == "max_parallel" and q_mp[0].get("model") == "granite4.2:8b" and mp["zwei"][1] > 3.0,
              f"{st_mp} {raw_mp[:80]!r} {mp} {q_mp}")
        obs = [d for d in state()["decisions"] if d.get("event") == "decision_observe" and d.get("t", 0) >= t_mp]
        check("0.6.7 Beobachtungsmodus: wartende Anfrage wird im Hintergrund eingeordnet (decision_observe, CPU-Kette, nur Protokoll)",
              len(obs) == 1 and obs[0].get("wait_reason") == "max_parallel" and obs[0].get("blocked_model") == "granite4.2:8b"
              and obs[0].get("class") in ("standard", "gross", "assist", "code") and obs[0].get("engine") != "local_llm"
              and "would_fallback" in obs[0] and obs[0].get("request_id") == q_mp[0].get("request_id"), str(obs))
        an_datei = os.path.join(HERE, "requests.jsonl")
        zeilen_an = [json.loads(z) for z in open(an_datei, encoding="utf-8")] if os.path.exists(an_datei) else []
        mine = [z for z in zeilen_an if q_mp and z.get("request_id") == q_mp[0].get("request_id")]
        check("0.6.7 observe_all: Zeile je Anfrage in requests.jsonl mit Klasse, Dauer, Wartezeit, Token - ohne Inhalte",
              len(mine) == 1 and mine[0].get("class") in ("standard", "gross", "assist", "code") and mine[0].get("duration_s", 0) > 1.5
              and mine[0].get("queued_s", 0) > 1.0 and mine[0].get("outcome") == "ok" and "output_tokens" in mine[0]
              and "zwei" not in json.dumps(mine[0]), f"{len(zeilen_an)} Zeilen, {mine}")
        st_rs, raw_rs = http(C + "/admin/config", {"models": {"granite4.2:8b": granite}}, method="PUT")
        check("... Grenze per UI wieder entfernt", st_rs == 200 and not json.loads(http(C + "/admin/config")[1])["models"]["granite4.2:8b"].get("max_parallel"),
              f"{st_rs} {raw_rs[:100]!r}")
        t_mp = time.time()
        mp.clear()
        ths = [threading.Thread(target=mp_los, args=("drei", 0)), threading.Thread(target=mp_los, args=("vier", 0.4))]
        for t_ in ths:
            t_.start()
        for t_ in ths:
            t_.join(30)
        q_mp = [d for d in state()["decisions"] if d.get("event") == "queued" and d.get("t", 0) >= t_mp]
        check("... ohne max_parallel laufen beide gleichzeitig (kein queued, zweite < 3 s)", not q_mp and mp.get("vier", (0, 9))[1] < 3.0,
              f"{mp} {q_mp}")
        # Ollama aborts mid-stream with {"error": ...} (2026-10-02: broken tool XML) - until 0.6.3 this counted as ok
        t_se = time.time()
        st, raw = http(R + "/api/chat", {"model": "klein", "stream": True, "messages": [{"role": "user", "content": "x [[stream_error]]"}]})
        zeilen = [json.loads(z) for z in raw.decode().splitlines() if z.strip()]
        se = [d for d in state()["decisions"] if d.get("event") == "stream_error" and d.get("t", 0) >= t_se]
        check("Stream-Fehler (Ollama-Format): Fehlerzeile im Wortlaut an den Client, stream_error im Entscheidungslog", st == 200
              and zeilen and zeilen[-1] == {"error": "XML syntax error on line 3: unexpected EOF"} and len(se) == 1
              and "XML syntax error" in se[0].get("error", "") and se[0].get("node") == "small", f"{zeilen[-1:]} {se}")
        st, raw = http(R + "/v1/messages", {**an_body, "stream": True, "messages": [{"role": "user", "content": "x [[stream_error]]"}]},
                       headers=an_hdr)
        evs = sse_events(raw)
        check("Stream-Fehler (Anthropic): event error api_error mit Ollamas Text, danach nichts mehr", st == 200 and evs
              and evs[-1][0] == "error" and evs[-1][1]["error"]["type"] == "api_error" and "XML syntax error" in evs[-1][1]["error"]["message"],
              str([e for e, _ in evs]))
        st, raw = http(R + "/v1/chat/completions", {"model": "klein", "stream": True, "messages": [{"role": "user", "content": "x [[stream_error]]"}]})
        datas = [l[6:] for l in raw.decode().split("\n") if l.startswith("data: ")]
        errs = [json.loads(d)["error"] for d in datas if d != "[DONE]" and "error" in json.loads(d)]
        check("Stream-Fehler (OpenAI): error-Objekt im Stream, [DONE] am Ende", st == 200 and len(errs) == 1
              and "XML syntax error" in errs[0]["message"] and errs[0]["type"] == "api_error" and datas[-1] == "[DONE]", str(datas[-3:]))
        st, raw = http(R + "/v1/messages", {**an_body, "stream": True, "messages": [{"role": "user", "content": "hi [[sleep_s=2.5]]"}]}, headers=an_hdr)
        names = [e for e, _ in sse_events(raw)]
        first_block = names.index("content_block_start") if "content_block_start" in names else -1
        check("/v1/messages stream, langes Prefill: message_start sofort, dann ping, dann Inhalt", st == 200 and names[0] == "message_start"
              and first_block > 1 and "ping" in names[1:first_block] and names[-1] == "message_stop", str(names))
        st, raw = http(R + "/v1/messages", {**an_body, "stream": True, "messages": [{"role": "user", "content": "hi [[sleep_s=1.5]] [[fail_status=400]]"}]},
                       headers=an_hdr)
        evs = sse_events(raw)
        check("/v1/messages stream: Knoten lehnt nach dem Keep-alive ab -> event error im Stream", st == 200 and evs and evs[-1][0] == "error"
              and evs[-1][1]["error"]["type"] == "invalid_request_error" and "fake failure" in evs[-1][1]["error"]["message"], str([e for e, _ in evs]))
        st, raw = http(R + "/v1/messages/count_tokens", {"model": "klein", "messages": [{"role": "user", "content": "Hallo Welt " * 50}]}, headers=an_hdr)
        check("/v1/messages/count_tokens -> input_tokens (Schaetzung), nie 404", st == 200 and json.loads(raw).get("input_tokens", 0) > 50, raw.decode()[:100])
        st, raw = http(R + "/v1/messages", {**an_body, "model": "gibtsnicht"}, headers=an_hdr)
        check("/v1/messages unbekanntes Modell -> 404 not_found_error", st == 404 and json.loads(raw)["error"]["type"] == "not_found_error", raw.decode()[:120])
        st, raw = http(R + "/v1/messages", {"model": "klein", "max_tokens": 5}, headers=an_hdr)
        check("/v1/messages ohne messages -> 400 invalid_request_error", st == 400 and json.loads(raw) == {"type": "error", "error": {
            "type": "invalid_request_error", "message": "messages is required"}}, raw.decode()[:120])
        st, raw = http(R + "/v1/models", headers=an_hdr)
        amod = json.loads(raw)
        check("/v1/models mit anthropic-version: Anthropic-Form (type model, has_more)", st == 200 and amod.get("has_more") is False
              and any(m.get("type") == "model" and m.get("id") == "klein:latest" for m in amod.get("data", [])), raw.decode()[:160])
        st, raw = http(R + "/v1/files")
        check("/v1/<unbekannt> -> 404 OpenAI-Fehler", st == 404 and "not supported" in json.loads(raw)["error"]["message"], raw.decode()[:100])
        st, raw = http(C + "/admin/try", {"model": "klein:latest", "prompt": "hi"})
        j = json.loads(raw)
        check("/admin/try liefert Entscheidung + Antwort", st == 200 and j["status"] == 200 and j["decision"]["model"] == "granite4.2:8b" and "Hallo" in j["answer"], raw.decode()[:160])
        check("roles.yaml geschrieben", os.path.exists(os.path.join(HERE, "roles.yaml")))
        # Measurement: granite on small (big is offline) -> weights 4.95, kv 0.158 from the Fake
        st, raw = http(C + "/admin/measure", {"model": "granite4.2:8b"})
        check("/admin/measure startet auf small", st == 200 and json.loads(raw)["node"] == "small", raw.decode()[:100])
        for _ in range(40):
            if not state()["measuring"]: break
            time.sleep(0.5)
        cfg = json.loads(http(C + "/admin/config")[1])
        g = cfg["models"]["granite4.2:8b"]
        check("Messung: weights/kv aus /api/ps abgeleitet, Quelle gemessen", g.get("source") == "measured" and abs(g["kv_gib_per_1k"] - 0.158) < 0.01 and abs(g["weights_gib"] - 4.95) < 0.05, str(g))
        st, raw = http(C + "/admin/measure", {"model": "qwen3.6:35b-a3b"})
        check("Messung ohne freien Knoten mit dem Modell -> 409", st == 409, raw.decode()[:100])
        # Performance: passive from previous requests (Fake: 30 Token / 0.5 s = 60 tok/s)
        perf = state()["perf"]
        p = perf.get("granite4.2:8b@small")
        check("passive Leistungsdaten aus echten Anfragen (60 tok/s)", p is not None and p["passive"]["gen_tps"] == 60.0 and p["passive"]["n"] >= 2, str(p and p["passive"]))
        st, raw = http(C + "/admin/bench", {"model": "granite4.2:8b"})
        check("/admin/bench startet", st == 200 and json.loads(raw)["node"] == "small", raw.decode()[:100])
        for _ in range(40):
            if not state()["benching"]: break
            time.sleep(0.5)
        p = state()["perf"].get("granite4.2:8b@small")
        check("Benchmark gespeichert (Median gen tok/s, prompt tok/s)", p and p.get("bench") and p["bench"]["gen_tps"] == 60.0 and p["bench"]["prompt_tps"] == 1000.0, str(p and p.get("bench")))
        check("perf.json persistiert", os.path.exists(os.path.join(HERE, "perf.json")))
        st, raw = http(C + "/admin/ha")
        ha = json.loads(raw)
        # roles_total 9 = 8 from config (incl. canary, cloud, cloud2) + small from UI
        check("/admin/ha: Knoten/Modelle/Rollen aggregiert", st == 200 and ha["nodes_total"] == 2 and ha["nodes_online"] == 1
              and "granite4.2:8b" in ha["models"] and ha["roles_total"] == 9, json.dumps({k: ha[k] for k in ("nodes_online", "models_available", "roles_ready", "problem", "problems")}))
        check("/admin/ha: gross ohne Knoten -> problem=true mit Grund", ha["problem"] is True and any("gross" in p for p in ha["problems"]) and ha["roles"]["gross"]["ready"] is False, str(ha["problems"]))
        check("/admin/ha: klein bereit auf small", ha["roles"]["klein"]["ready"] is True and ha["roles"]["klein"]["node"] == "small", str(ha["roles"]["klein"]))
        check("/admin/ha: Anfragen der letzten 5 min gezaehlt", ha["requests_5min"] >= 5 and ha["last_route"].startswith("klein ->"), f"{ha['requests_5min']} {ha['last_route']}")
        # Agent failure: small reports once, then silence -> after agent_missing_problem_s (8 s) an HA Problem
        hb("small", 1, 1200, 6900, total=8192)
        ha = json.loads(http(C + "/admin/ha")[1])
        check("/admin/ha: frischer Heartbeat -> kein Agent-Problem", not any("Agent-Heartbeat" in p for p in ha["problems"]), str(ha["problems"]))
        time.sleep(9)
        ha = json.loads(http(C + "/admin/ha")[1])
        check("/admin/ha: Agent schweigt -> Problem 'kein Agent-Heartbeat'", any("small: kein Agent-Heartbeat" in p for p in ha["problems"]) and ha["problem"] is True, str(ha["problems"]))
        st, raw = http(C + "/v1/heartbeat/big", {"gpu_util_pct": 1}, headers={"X-Router-Token": "wrong"})
        check("Heartbeat mit falschem Token -> 401", st == 401)
        st, raw = http(R + "/api/pull", {"model": "x"})
        check("/api/pull -> 403", st == 403)
        # ---- Tier 2: Client authentication on the API port ------------------------------------------------
        def ca():
            return json.loads(http(C + "/admin/state")[1])["client_auth"]

        def hdr(tok):
            return {"Authorization": "Bearer " + tok}

        def chat_body(m):
            return {"model": m, "messages": [{"role": "user", "content": "hi"}], "stream": False}
        st, raw = http(R + "/api/tags")   # without identity, not exempt
        v = ca()
        check("client_auth observe: Unbekannte werden bedient und gezaehlt (IP, Pfad)", st == 200 and v["mode"] == "observe"
              and any(u["total"] >= 1 and u["last_path"] for u in v["unauthenticated"].values()), json.dumps(v["unauthenticated"])[:160])
        st, raw = http(R + "/api/tags", headers=hdr("tester-token"))
        t = ca()["clients"]["tester"]
        check("Bearer-Token identifiziert den Client", st == 200 and t["total"] >= 1 and t["via"] == "bearer", json.dumps(t))
        st, raw = http(R + "/api/tags", headers={"Authorization": "Basic " + base64.b64encode(b"tester:tester-token").decode()})
        check("Basic <client>:<token> identifiziert ebenfalls", st == 200 and ca()["clients"]["tester"]["via"] == "basic")
        st, raw = http(R + "/api/tags", headers={"x-api-key": "tester-token"})
        check("x-api-key (Anthropic-Clients) identifiziert ebenfalls", st == 200 and ca()["clients"]["tester"]["via"] == "x-api-key")
        st, raw = http(R + "/api/tags", headers=hdr("voellig-falsch"))
        check("falsches Token zaehlt als bad_token (kein Rueckfall auf die IP)", st == 200
              and any(u["bad_token"] >= 1 for u in ca()["unauthenticated"].values()))
        st, raw = http(R + "/api/chat", chat_body("standard:latest"), headers=hdr("restricted-token"))
        check("Rolle fuer diesen Client nicht erlaubt -> 403", st == 403 and "not allowed" in raw.decode(), f"{st} {raw[:90]}")
        st, raw = http(R + "/api/chat", chat_body("klein:latest"), headers=hdr("restricted-token"))
        check("erlaubte Rolle -> 200", st == 200, f"{st} {raw[:60]}")
        # concrete models are hidden since the UI test above (expose_concrete_models: False) -> for this check
        # enable, otherwise 404 instead of 403 (exactly what happened), then disable again
        http(C + "/admin/config", {"expose_concrete_models": True}, method="PUT")
        st, raw = http(R + "/api/chat", chat_body("granite4.2:8b"), headers=hdr("restricted-token"))
        nd = {k: (v["state"], sorted(v["models"])) for k, v in state()["nodes"].items()}   # Context for the error case
        check("konkretes Modell fuer diesen Client gesperrt -> 403", st == 403 and "concrete model names" in raw.decode(),
              f"{st} {raw[:90]} | Knoten: {nd}")
        http(C + "/admin/config", {"expose_concrete_models": False}, method="PUT")
        codes = [http(R + "/api/tags", headers=hdr("limited-token"))[0] for _ in range(4)]
        check("Rate Limit 3/min: die vierte Anfrage bekommt 429", codes == [200, 200, 200, 429], str(codes))
        # client_auth.locked (Audit 2026-09-16): locked mode is not changeable via Settings, config carries the flag
        sys.path.insert(0, os.path.join(HERE, "..", "router"))
        from skirnir_router import settings as _smod   # noqa: E402
        from skirnir_router.config import Config as _Cfg   # noqa: E402
        locked_cfg = {"router": {"client_auth": {"mode": "enforce", "locked": True}}}
        refused = _smod.coerce("router.client_auth.mode", "observe", locked_cfg)   # 0.8.9: Problem statt ValueError
        lock_err = getattr(refused, "message", "") if getattr(refused, "status", None) == 400 else ""
        unlocked_ok = _smod.coerce("router.client_auth.mode", "observe", {"router": {"client_auth": {"mode": "enforce"}}}) == "observe"
        check("client_auth.locked: Settings lehnen den Modus ab, ungesperrt geht er durch", "locked" in lock_err and unlocked_ok, lock_err[:80])
        _yaml_txt = open(os.path.join(HERE, "test-config.yaml"), encoding="utf-8").read()
        _c = yaml.safe_load(_yaml_txt); _c.setdefault("router", {}).setdefault("client_auth", {})["locked"] = True
        _lp = os.path.join(HERE, "locked-config.yaml"); open(_lp, "w", encoding="utf-8").write(yaml.safe_dump(_c, allow_unicode=True, sort_keys=False))
        _cfg = _Cfg(_lp, use_overrides=False)
        check("client_auth.locked wird geparst", _cfg.client_auth.get("locked") is True, str(_cfg.client_auth.get("locked")))
        st, raw = http(C + "/admin/client_auth", {"mode": "enforce"})
        st1, raw1 = http(R + "/api/tags")
        st2, raw2 = http(R + "/v1/models")
        st3, _ = http(R + "/api/version")
        st4, _ = http(R + "/api/tags", headers=hdr("tester-token"))
        check("enforce: ohne Token 401 im Ollama- und OpenAI-Format, /api/version frei, mit Token 200",
              st == 200 and st1 == 401 and "error" in json.loads(raw1) and st2 == 401 and "error" in json.loads(raw2)
              and st3 == 200 and st4 == 200, f"{st1} {raw1[:60]} | {st2} {raw2[:60]} | {st3} {st4}")
        http(C + "/admin/client_auth", {"mode": "observe"})
        check("zurueck auf observe", ca()["mode"] == "observe" and ca()["configured_mode"] == "observe")
        alog = open(os.path.join(HERE, "audit.jsonl"), encoding="utf-8").read()
        check("Audit-Log: auth_missing, forbidden, rate_limited, auth_denied, client_auth_mode protokolliert",
              all(e in alog for e in ("auth_missing", "forbidden", "rate_limited", "auth_denied", "client_auth_mode")), alog[-240:])

        # --- Agent update via router (agentupdate.py): Manifest signed by operator key, trigger via UI button,
        # Fake agent checks signature/hash, loads with one-time token, reports status in heartbeat and registers with new version.
        from cryptography.hazmat.primitives import serialization as _ser
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as _Ed
        _upk = _Ed.generate()
        open(os.path.join(HERE, "agent-update.pub"), "w", encoding="utf-8").write(base64.b64encode(_upk.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)).decode())
        _adir = os.path.join(HERE, "agent"); os.makedirs(_adir, exist_ok=True)
        def _manifest(version, sign_with):
            name = f"skirnir-agent-{version}-windows-amd64.exe"
            blob = f"fake agent {version}".encode() * 100
            open(os.path.join(_adir, name), "wb").write(blob)
            man = {"version": version, "generated": "2026-09-25T10:00:00", "files": [{"name": name, "os": "windows", "arch": "amd64", "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}]}
            canon = json.dumps(man, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            json.dump({"manifest": man, "signature": base64.b64encode(sign_with.sign(canon)).decode()}, open(os.path.join(_adir, "manifest.json"), "w", encoding="utf-8"))
        _manifest("test2", _upk)
        fp_big = next(n["fp"] for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")
        reg_big = lambda: next(n for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")  # noqa: E731
        am = json.loads(http(C + "/admin/agent-update")[1])
        check("Agent-Update: Manifest sichtbar, Knoten zeigt verfuegbare Version", am["manifest"]["version"] == "test2" and am["signed"] and reg_big()["update"]["pending"] and reg_big()["update"]["available"] == "test2", str(reg_big()["update"]))
        st, raw = http(C + f"/admin/nodes/{fp_big}/update", {})
        drained = state()["nodes"]["big"]["draining"]
        check("Agent-Update: Auftrag angenommen, Knoten im Drain", st == 200 and drained, raw.decode()[:100])
        for _ in range(40):
            u = reg_big()["update"]
            if u.get("state") in ("done", "failed"): break
            time.sleep(0.25)
        check("Agent-Update: Fake-Agent hat geprueft, geladen, gemeldet und laeuft mit der neuen Version", u.get("state") == "done" and reg_big()["facts"]["agent_version"] == "test2" and not state()["nodes"]["big"]["draining"], str(u))
        evs = [d["state"] for d in state()["decisions"] if d.get("event") == "agent_update"]
        check("Agent-Update: Ereignisse requested -> downloading -> applied -> done", evs[-4:] == ["requested", "downloading", "applied", "done"], str(evs))
        st, raw = http(C + f"/admin/nodes/{fp_big}/update", {})
        check("Agent-Update: gleiche Version -> 409", st == 409 and "already" in raw.decode(), raw.decode()[:80])
        # Error case: Manifest signed with foreign key -> Agent rejects, HA Problem
        _manifest("test3", _Ed.generate())
        st, raw = http(C + f"/admin/nodes/{fp_big}/update", {})
        for _ in range(40):
            u = reg_big()["update"]
            if u.get("state") in ("done", "failed") and u.get("version") == "test3": break
            time.sleep(0.25)
        ha_u = json.loads(http(C + "/admin/ha")[1])
        from skirnir_router import agentupdate as _au   # pure function: wait time from stored state
        check("Agent-Update: falsche Signatur -> failed mit kind leer (0.8.12: Feld aus dem Heartbeat), naechster Versuch erst nach 6 h",
              u.get("kind") == "" and _au.retry_after(u) == 6 * 3600, str(u))
        check("Agent-Update: falsche Signatur -> failed, HA-Problem, Version unveraendert, kein Drain", st == 200 and u.get("state") == "failed" and "Signatur" in (u.get("message") or "")
              and any("Agent-Update auf big fehlgeschlagen" in p for p in ha_u["problems"]) and reg_big()["facts"]["agent_version"] == "test2" and not state()["nodes"]["big"]["draining"], str(u) + str(ha_u["problems"]))
        st, raw = http(C + "/v1/agent/binary/skirnir-agent-test3-windows-amd64.exe", headers={"Authorization": "Bearer falsch"})
        check("Agent-Update: Download ohne gueltiges Token -> 403", st == 403, str(st))

        # --- Ollama update via router (ollamaupdate.py, 0.3.0): Version check against a Fake source (GitHub-API format),
        # Trigger via UI button, Fake agent loads, checks hash, reports status and Ollama version in heartbeat.
        import http.server as _hs
        _rel = {"version": "0.99.0", "blob": b"fake ollama 0.99.0" * 50}
        _rel["sum"] = hashlib.sha256(_rel["blob"]).hexdigest()
        class _RelH(_hs.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                if self.path == "/release":
                    body = json.dumps({"tag_name": "v" + _rel["version"], "assets": [
                        {"name": "sha256sum.txt", "browser_download_url": "http://127.0.0.1:21030/sha256sum.txt"},
                        {"name": "ollama-windows-amd64.zip", "browser_download_url": "http://127.0.0.1:21030/ollama-windows-amd64.zip", "size": len(_rel["blob"])}]}).encode()
                elif self.path == "/sha256sum.txt":
                    body = f"{_rel['sum']}  ./ollama-windows-amd64.zip\n".encode()
                elif self.path == "/ollama-windows-amd64.zip":
                    body = _rel["blob"]
                else:
                    body = None
                self.send_response(200 if body is not None else 404); self.send_header("Content-Length", str(len(body or b""))); self.end_headers(); self.wfile.write(body or b"")
        _rs = _hs.ThreadingHTTPServer(("127.0.0.1", 21030), _RelH)
        threading.Thread(target=_rs.serve_forever, daemon=True).start()
        st, raw = http(C + "/admin/config", {"settings": {"modes.ollama_update.release_url": "http://127.0.0.1:21030/release", "modes.ollama_update.window_start": "", "modes.ollama_update.window_end": ""}}, method="PUT")
        check("Ollama-Update: Quelle und Fenster per Einstellungen setzbar", st == 200, raw.decode()[:100])
        st, raw = http(C + "/admin/ollama-update/check", {})
        ou = json.loads(raw)
        check("Ollama-Update: Versionspruefung liefert 0.99.0 mit Archiv windows/amd64 und Hash aus sha256sum.txt", st == 200 and ou["latest"]["version"] == "0.99.0" and ou["latest"]["files"]["windows/amd64"]["sha256"] == _rel["sum"], raw.decode()[:160])
        rb = reg_big()["ollama_update"]
        check("Ollama-Update: Knoten big zeigt laufende und ausstehende Version", rb["pending"] and rb["available"] == "0.99.0" and bool(rb["current"]), str(rb))
        check("Ollama-Update: Metrik und HA-Sensor zaehlen den ausstehenden Knoten", "skirnir_node_ollama_update_pending{node=\"big\"} 1" in http(C + "/metrics")[1].decode() and json.loads(http(C + "/admin/ha")[1])["ollama_updates_pending"] >= 1)
        st, raw = http(C + f"/admin/nodes/{fp_big}/ollama-update", {})
        check("Ollama-Update: Auftrag angenommen, Knoten bleibt waehrend des Downloads im Routing (Agent 0.15.0 wartet auf Freigabe)",
              st == 200 and not state()["nodes"]["big"]["draining"], raw.decode()[:100])
        drained_ready = False
        for _ in range(80):
            rb = reg_big()["ollama_update"]
            if rb.get("state") == "ready" and state()["nodes"]["big"]["draining"]:
                drained_ready = True
            if rb.get("state") in ("done", "failed"): break
            time.sleep(0.25)
        rlog.seek(0)
        check("Ollama-Update: erst bei 'ready' aus dem Routing, dann Tausch freigegeben (Knoten leer)",
              "Ollama swap to 0.99.0 released (node idle)" in rlog.read(), f"draining bei ready gesehen: {drained_ready}")
        check("Ollama-Update: Fake-Agent hat geladen, Hash geprueft, gemeldet; Version 0.99.0 im Heartbeat -> done, kein Drain", rb.get("state") == "done" and rb["current"] == "0.99.0" and not rb["pending"] and not state()["nodes"]["big"]["draining"], str(rb))
        evs = [d["state"] for d in state()["decisions"] if d.get("event") == "ollama_update"]
        check("Ollama-Update: Ereignisse requested -> downloading -> ready -> applied -> done", evs[-5:] == ["requested", "downloading", "ready", "applied", "done"], str(evs))
        st, raw = http(C + f"/admin/nodes/{fp_big}/ollama-update", {})
        check("Ollama-Update: gleiche Version -> 409", st == 409 and "already" in raw.decode(), raw.decode()[:80])
        ha_o = json.loads(http(C + "/admin/ha")[1])
        check("Ollama-Update: HA-Snapshot kennt die neueste Version, nichts mehr ausstehend", ha_o.get("ollama_latest") == "0.99.0" and ha_o.get("ollama_updates_pending") == 0 and ha_o["nodes"]["big"].get("ollama_version") == "0.99.0", str({k: ha_o.get(k) for k in ("ollama_latest", "ollama_updates_pending")}))
        # Error case: source delivers 0.99.1, but archive does not match sha256sum.txt -> Agent rejects, HA Problem, version remains
        _rel["version"], _rel["blob"] = "0.99.1", b"manipuliert" * 50
        http(C + "/admin/ollama-update/check", {})
        st, raw = http(C + f"/admin/nodes/{fp_big}/ollama-update", {})
        for _ in range(60):
            rb = reg_big()["ollama_update"]
            if rb.get("state") in ("done", "failed") and rb.get("version") == "0.99.1": break
            time.sleep(0.25)
        ha_o = json.loads(http(C + "/admin/ha")[1])
        check("Ollama-Update: Hash-Widerspruch -> failed, HA-Problem, Version unveraendert, kein Drain", st == 200 and rb.get("state") == "failed" and "SHA-256" in (rb.get("message") or "")
              and any("Ollama-Update auf big fehlgeschlagen" in p for p in ha_o["problems"]) and rb["current"] == "0.99.0" and not state()["nodes"]["big"]["draining"], str(rb) + str(ha_o["problems"]))
        # Tray app node (0.3.3): agent reports at registration whether Ollama is its child (fact ollama_managed). Second
        # Fake agent 'tray' with ollama_managed=false: no button, no trigger (even with force), not counted in HA, no HA Problem.
        from skirnir_router import ollamaupdate as _ou   # pure functions: without fact (Agent < 0.12.1) no triggers
        check("Ollama-Update: managed() nur mit Fakt ollama_managed=true; ohne Fakt Grund 'agent', mit false Grund 'tray'",
              _ou.managed({"facts": {"ollama_managed": True}}) and not _ou.managed({"facts": {}}) and _ou.unmanaged_reason({"facts": {}}) == "agent"
              and _ou.unmanaged_reason({"facts": {"ollama_managed": False}}) == "tray")
        logs["agent-tray"] = open(os.path.join(HERE, "fake-agent-tray.log"), "w+")
        tray = subprocess.Popen([PY, os.path.join(HERE, "fake_agent.py"), "wss://127.0.0.1:21435", "tray", "http://127.0.0.1:21002", os.path.join(HERE, "fake-agent-tray.key")],
                                stdout=logs["agent-tray"], stderr=subprocess.STDOUT, cwd=HERE, env={**os.environ, "FAKE_OLLAMA_MANAGED": "0", "FAKE_OLLAMA_PUSH": "0"})
        procs.append(tray)
        reg_tray = lambda: next((n for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "tray"), None)  # noqa: E731
        for _ in range(40):
            if reg_tray(): break
            time.sleep(0.25)
        fp_tray = reg_tray()["fp"]
        http(C + f"/admin/nodes/{fp_tray}/approve", {"mqtt": False})
        for _ in range(40):
            rt = reg_tray()
            if rt and rt["connected"] and rt["state"] == "approved": break
            time.sleep(0.25)
        ot = rt["ollama_update"]
        check("Agent ohne Ollama-Push (vor 0.14.0): der Router pollt ihn weiter", state()["nodes"].get("tray", {}).get("ollama_source") == "poll",
              str(state()["nodes"].get("tray", {}).get("ollama_source")))
        check("Ollama-Update: Tray-Knoten meldet ollama_managed=false -> nicht verwaltet (Grund tray), ausstehend, aber nicht in HA gezaehlt",
              rt["facts"].get("ollama_managed") is False and ot["managed"] is False and ot["unmanaged_reason"] == "tray" and ot["pending"]
              and "tray" not in json.loads(http(C + "/admin/ha")[1]).get("ollama_updates", {}), str(ot))
        st, raw = http(C + f"/admin/nodes/{fp_tray}/ollama-update", {})
        st2, raw2 = http(C + f"/admin/nodes/{fp_tray}/ollama-update", {"force": True})
        check("Ollama-Update: Tray-Knoten -> 409, auch mit force (der Agent koennte es nicht)", st == 409 and st2 == 409 and "not a child" in raw.decode(), raw.decode()[:100] + raw2.decode()[:60])
        check("Ollama-Update: Tray-Knoten erzeugt kein HA-Problem zum Ollama-Update", not any("Ollama-Update auf tray" in p for p in json.loads(http(C + "/admin/ha")[1])["problems"]))
        http(C + f"/admin/nodes/{fp_tray}/revoke", {})
        http(C + f"/admin/nodes/{fp_tray}", method="DELETE")
        tray.terminate(); tray.wait()
        st, raw = http(C + "/admin/config", {"settings": {"modes.ollama_update.window_start": "03:00", "modes.ollama_update.window_end": "03:01"}}, method="PUT")
        ou = json.loads(http(C + "/admin/ollama-update")[1])
        check("Ollama-Update: Nachtfenster aus den Einstellungen sichtbar, jetzt ausserhalb", st == 200 and ou["window_start"] == "03:00" and ou["in_window"] is (time.localtime().tm_hour == 3 and time.localtime().tm_min == 0), str({k: ou[k] for k in ("window_start", "window_end", "in_window")}))
        http(C + "/admin/config", {"settings": {"modes.ollama_update.release_url": None, "modes.ollama_update.window_start": None, "modes.ollama_update.window_end": None}}, method="PUT")
        _rs.shutdown()

        # Metrics persistent (0.1.9): metrics.json next to config, saved 60 s after first change (tick_loop); a fresh
        # process loads counters and histograms and serves same values in /metrics
        mp = os.path.join(HERE, "metrics.json")
        check("metrics.json: liegt neben der Config (Lauf > 60 s, tick_loop hat gesichert)", os.path.exists(mp), mp)
        mtxt = http(C + "/metrics")[1].decode()
        rc_m = subprocess.run([PY, "-c", "import sys, json; sys.path.insert(0, sys.argv[1]); from skirnir_router import state, config, metrics; "
                               "state.CFG = config.Config(sys.argv[2]); n = metrics.metrics_load(); "
                               "c = state.METRICS['counters'].get('skirnir_requests_total', {}); print(n, round(sum(c.values())), json.load(open(sys.argv[3]))['since'][:4])",
                               os.path.join(HERE, "..", "router"), os.path.join(HERE, "test-config.yaml"), mp], capture_output=True, text=True)
        live_total = round(sum(float(l.split()[-1]) for l in mtxt.splitlines() if l.startswith("skirnir_requests_total{")))
        parts = rc_m.stdout.split()
        check("metrics.json: frischer Prozess laedt die Reihen, skirnir_requests_total stimmt bis auf die letzte Minute", rc_m.returncode == 0 and len(parts) == 3 and int(parts[0]) > 10
              and 0 < int(parts[1]) <= live_total and parts[2] == "2026", (rc_m.stdout + rc_m.stderr)[-200:] + f" live={live_total}")
        check("/admin/state: metrics_since gesetzt", str(state().get("metrics_since") or "").startswith("2026"), str(state().get("metrics_since")))

        # Locks: node disappears from routing, agent gets 'revoked'; then delete
        fp = next(n["fp"] for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")
        st, raw = http(C + f"/admin/nodes/{fp}/revoke", {})
        time.sleep(1.5)
        logs["agent-big"].seek(0); alog = logs["agent-big"].read()
        check("Sperren: big aus dem Routing, Register 'revoked', Agent informiert", st == 200 and "big" not in state()["nodes"]
              and next(n["state"] for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["fp"] == fp) == "revoked" and '"status": "revoked"' in alog, alog[-160:])
        # Stop agent first: it reconnects every 0.5 s and would immediately re-register the deleted key
        # as "waiting for approval" - the check failed sporadically because of this.
        agent.terminate(); agent.wait()
        time.sleep(1)
        st, raw = http(C + f"/admin/nodes/{fp}", method="DELETE")
        nodes = json.loads(http(C + "/admin/nodes")[1])["nodes"]
        check("Registereintrag geloescht", st == 200 and not any(n["fp"] == fp for n in nodes), f"{raw.decode()[:60]} {[n['name'] for n in nodes]}")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try: p.wait(timeout=5)
            except Exception: p.kill()  # noqa: BLE001
    rlog.seek(0)
    tail = rlog.read().splitlines()[-12:]
    print("\n--- router.log (tail) ---"); print("\n".join(tail))
    print(f"\n{len(FAILS)} failures: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
