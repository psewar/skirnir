#!/usr/bin/env python3
"""End-to-End-Tests gegen den Router mit zwei Fake-Ollama-Knoten. Start: python run_tests.py"""
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


LAST_HEADERS = {}   # Antwort-Header der letzten http()-Anfrage (Stufe 1: X-Skirnir-*)


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


REQ_T = 0.0   # Zeitpunkt der letzten Anfrage, fuer route()


class Decision(dict):
    """dict, das bei fehlenden Schluesseln None liefert: fand route() nichts, soll die Pruefung sauber scheitern
    statt den ganzen Lauf mit KeyError abzubrechen."""

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
    """Die Routing-Entscheidung zur letzten Anfrage (oder ab `since`).

    NICHT decisions[-1] nehmen: dazwischen landen free-, busy-, prewarm- oder unload-Ereignisse, die nichts mit
    der eigenen Anfrage zu tun haben - genau daran ist der Test 'warm zuerst' sporadisch gescheitert.
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
    # selbstsigniertes Zertifikat für den TLS-Control-Port (openssl aus Git for Windows / System)
    if not os.path.exists(os.path.join(HERE, "test-cert.pem")):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-keyout", "test-key.pem", "-out", "test-cert.pem", "-days", "30", "-subj", "/CN=localhost"],
                       cwd=HERE, check=True, capture_output=True)
    for f in ("roles.yaml", "perf.json", "nodes.json", "fake-agent-big.key", "audit.jsonl", "usage.json", "decisions.jsonl", "events.jsonl", "metrics.json", "agent-update.pub", "agent/manifest.json"):   # usage.json: sonst zaehlen Cloud-Kosten des Vorlaufs mit   # Reste aus fruehrem Lauf entfernen
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
    logs["cloud"] = open(os.path.join(HERE, "fake-cloud.log"), "w+")   # Stufe 5: OpenAI- und Anthropic-Fake auf einem Port
    procs.append(subprocess.Popen([PY, os.path.join(HERE, "fake_cloud.py"), "21010", "cloud-test-key", "claude-test-key"], cwd=HERE,
                                  stdout=logs["cloud"], stderr=subprocess.STDOUT))
    open(os.path.join(HERE, "secrets.env"), "w", encoding="utf-8").write("CLOUD_TEST_KEY=cloud-test-key\nCLAUDE_TEST_KEY=claude-test-key\n")
    rlog = open(os.path.join(HERE, "router.log"), "w+")
    router = subprocess.Popen([PY, os.path.join(HERE, "..", "router", "router.py"), os.path.join(HERE, "test-config.yaml")],
                              stdout=rlog, stderr=subprocess.STDOUT, cwd=HERE)
    procs.append(router)
    # big kommt per Tunnel: Fake-Agent verbindet sich zum Router und reicht an das Fake-Ollama (TLS + Token) weiter
    logs["agent-big"] = open(os.path.join(HERE, "fake-agent-big.log"), "w+")
    agent = subprocess.Popen([PY, os.path.join(HERE, "fake_agent.py"), "wss://127.0.0.1:21435", "big", "https://127.0.0.1:21001", os.path.join(HERE, "fake-agent-big.key"), "testtoken"],
                             stdout=logs["agent-big"], stderr=subprocess.STDOUT, cwd=HERE)
    procs.append(agent)
    try:
        # Registrierung: big kennt der Router nicht -> Schluessel landet als "wartet auf Freigabe" im Register
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

        st, raw = http(R + "/api/tags")
        names = [m["name"] for m in json.loads(raw)["models"]]
        roles = [n for n in names if n.split(":")[0] in ("assist", "code", "gross", "standard")]
        check("/api/tags zeigt Rollen + konkrete Modelle", len(roles) == 4 and "qwen3-coder:30b" in names and "granite4.2:8b" in names, str(names))

        st, raw = http(R + "/api/show", {"model": "standard"})
        check("/api/show für Rolle ohne Tag", st == 200 and "tools" in raw.decode(), f"{st}")

        time.sleep(5)   # prewarm wartet bis 4 s auf einen Heartbeat (keiner da -> Knoten ohne Agent)
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
        check("ctx = min(client 8192, tier 65536)", d["ctx"] == 8192, str(d["ctx"]))

        time.sleep(1.5)  # Poll holt /api/ps -> qwen geladen
        hb("big", 2, 26600, 6000)   # 5.9 GiB frei: qwen (21.4) passt nur, weil geladenes Ollama-VRAM (20.6) verdrängbar zählt
        st, txt = chat("local-assist")
        d = route()
        check("Alias (gleicher Digest) als konkretes Modell -> warm auf big", st == 200 and d["node"] == "big" and d["warm"] is True, str(d))
        logs["big"].seek(0); sent = last_route(logs["big"].read())[-1]
        # zuletzt wurde qwen mit num_ctx 8192 (non-stream-Test) geladen -> Alias-Request muss 8192 senden, nicht Ollama-Default
        check("Alias ohne num_ctx uebernimmt geladenen Kontext (8192, kein Reload)", sent["num_ctx"] == 8192, str(sent))
        # Fremd-VRAM-Transiente (Modellwechsel): ein einzelner Ausreisser darf nicht busy machen
        hb("big", 5, 30000, 2600); time.sleep(0.6); hb("big", 5, 26600, 6000)
        check("einzelner Fremd-VRAM-Ausreisser bleibt free", state()["nodes"]["big"]["state"] == "free", state()["nodes"]["big"]["busy_reason"])
        # GPU-Schutz (Agent >= 0.7.0): Status im Heartbeat -> Deckel, HA-Problem, Ereignis; kein Einfluss auf free/busy
        base_cap = state()["nodes"]["big"]["max_inflight"]
        st, txt = hb("big", 5, 26600, 6000, gpu_guard={"state": "gedrosselt", "limit_w": 403, "target_w": 403, "default_limit_w": 575, "problem": False, "quelle": "nvml"})
        n = state()["nodes"]["big"]
        check("GPU-Schutz gedrosselt -> Deckel 1 gleichzeitig, free bleibt", n["max_inflight"] == 1 and n["gpu_guard"]["state"] == "gedrosselt" and n["state"] == "free", str(n.get("gpu_guard")))
        check("Heartbeat-Antwort traegt gpu_guard_ack", st == 200 and json.loads(txt).get("gpu_guard_ack") is True, txt[:120])
        hb("big", 5, 26600, 6000, gpu_guard={"state": "unverfuegbar", "problem": True, "grund": "Limit nicht setzbar: keine Berechtigung (rc 4)"})
        ha_s = json.loads(http(C + "/admin/ha")[1])
        check("GPU-Schutz unverfuegbar -> HA-Problem, Deckel wieder normal", any("GPU-Schutz unverfuegbar" in p for p in ha_s["problems"]) and state()["nodes"]["big"]["max_inflight"] == base_cap, str(ha_s["problems"]))
        hb("big", 5, 26600, 6000, gpu_guard={"state": "aus", "problem": True, "grund": "abgewaehlt"})
        ha_s = json.loads(http(C + "/admin/ha")[1])
        check("GPU-Schutz abgewaehlt -> HA-Problem (Betreiber-Entscheid)", any("abgewaehlt" in p for p in ha_s["problems"]), str(ha_s["problems"]))
        hb("big", 5, 26600, 6000, gpu_guard={"state": "normal", "limit_w": 460, "target_w": 460, "problem": False, "quelle": "nvml+gpuz"})
        ha_s = json.loads(http(C + "/admin/ha")[1])
        check("GPU-Schutz normal -> kein Problem, Deckel normal, HA-Knoten traegt Limit", not any("GPU-Schutz" in p for p in ha_s["problems"]) and state()["nodes"]["big"]["max_inflight"] == base_cap and ha_s["nodes"]["big"]["gpu_guard"]["limit_w"] == 460, str(ha_s["nodes"]["big"].get("gpu_guard")))
        evs = [d for d in state()["decisions"] if d.get("event") == "gpu_guard"]
        check("gpu_guard-Ereignisse im Entscheidungsprotokoll (je Zustandswechsel)", len(evs) >= 4 and evs[-1]["state"] == "normal", str([e["state"] for e in evs]))
        m_txt = http(C + "/metrics")[1]
        m_txt = m_txt.decode() if isinstance(m_txt, bytes) else str(m_txt)
        check("Metrik skirnir_node_gpu_guard", 'skirnir_node_gpu_guard{node="big",state="normal"} 1' in m_txt, "")
        # Entscheidungsprotokoll persistent: events.jsonl neben der Config, eine Zeile je Eintrag; ein frischer Prozess laedt es
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

        st, txt = chat("code:latest", ctx=65536)
        d = route()
        check("code 65536: 17.3+6.3+0.8=24.4 passt in 31.8-1 -> tier0", d["tier"] == 0 and d["model"] == "qwen3-coder:30b", str(d))

        # big wird busy: 60 % Auslastung, 2 s sustain
        # Erst: nur Auslastung, kein fremdes VRAM (= anderer Ollama-Client) -> darf NICHT busy werden
        for _ in range(4):
            hb("big", 65, 31300, 1300); time.sleep(0.7)    # 30.6 GiB belegt = 23.6 coder@65k (17.3+0.096*65.5) + 7 Baseline -> foreign 0
        check("Auslastung ohne fremdes VRAM bleibt free (anderer Ollama-Client)", state()["nodes"]["big"]["state"] == "free")
        # Dann: Spiel -> Auslastung + 1.7 GiB fremdes VRAM
        for _ in range(4):
            hb("big", 65, 33400, 0); time.sleep(0.7)       # 32.6 = 23.6 + 7 + 2.0 fremd
        s = state()
        check("big -> busy durch gpu_util (+fremdes VRAM)", s["nodes"]["big"]["state"] == "busy",
              json.dumps({k: s["nodes"]["big"].get(k) for k in ("state", "busy_reason", "gpu_util", "vram_used_gib", "vram_free_gib", "foreign_vram_gib", "baseline_gib", "loaded", "inflight", "gpu_known", "heartbeat_age_s", "breaker")}))
        time.sleep(0.5)
        for _ in range(2):
            hb("big", 65, 9000, 23600); time.sleep(0.5)    # qwen entladen: 8.8 GiB = 7 Baseline + 1.8 Spiel
        logs["big"].seek(0); out = logs["big"].read()
        check("unload_on_busy entlädt das grosse Modell (nicht granite)", '"unload": "qwen3-coder:30b"' in out and '"unload": "granite4.2:8b"' not in out)

        st, txt = chat("standard:latest", ctx=65536)
        d = route()
        check("standard bei busy big -> granite tier1 (busy_ok)", st == 200 and d["model"] == "granite4.2:8b" and d["tier"] == 1, str(d))
        # big busy: 20.1-2 = 18.1 GiB Budget -> granite@32768 (4.95+5.2+0.8=10.9) passt auf big; small (8-1=7) nicht
        check("... und zwar auf big (small zu klein für 32k)", d["node"] == "big", d["node"])
        st, txt = chat("gross:latest")
        check("gross bei busy big ohne Fallback -> 503", st == 503, f"{st} {txt[:80]}")
        ha = json.loads(http(C + "/admin/ha")[1])
        check("/admin/ha bei busy: gross ist 'limited_by_busy', KEIN Problem", ha["roles"]["gross"]["ready"] is False
              and ha["roles"]["gross"].get("limited_by_busy") is True and "gross" in ha["roles_limited"]
              and not any("gross" in p for p in ha["problems"]), json.dumps({"problems": ha["problems"], "limited": ha["roles_limited"]}))

        # Sicherheitsnetz: grosses Modell landet trotz busy im VRAM (z. B. Load-Race) -> Router entlaedt es nachtraeglich.
        # Heartbeats laufen dabei durchgehend weiter (sonst wird der Knoten durch Heartbeat-Staleness kurz free).
        for _ in range(3):
            hb("big", 65, 33400, 0); time.sleep(0.7)
        def unloads():
            logs["big"].seek(0); return logs["big"].read().count('"unload": "qwen3-coder:30b"')
        before = unloads()
        http("https://127.0.0.1:21001/api/generate", {"model": "qwen3-coder:30b", "keep_alive": -1, "options": {"num_ctx": 8192}}, headers={"X-Router-Token": "testtoken"}, auth=False)
        after = before
        # bis ~7 s: Mindestabstand des Sicherheitsnetzes (unload_on_busy_interval_s, im Test 2 s) + Poll (1 s) +
        # asynchroner Unload; used > total, damit trotz Extra-Modell fremdes VRAM bleibt
        for _ in range(10):
            hb("big", 65, 45000, 0); time.sleep(0.7)
            after = unloads()
            if after > before:
                break
        check("busy-Sicherheitsnetz: nachtraeglich geladenes grosses Modell wird wieder entladen", after > before and state()["nodes"]["big"]["state"] == "busy",
              f"unloads {before} -> {after}, state {state()['nodes']['big']['state']}")
        # busy vorbei: Heartbeats bis der Knoten free ist, dann SOFORT weiter (die naechste Pruefung muss vor dem
        # prewarm-on-free liegen, der 6 s nach dem Wechsel startet)
        for _ in range(12):
            hb("big", 3, 8000, 24600); time.sleep(0.7)
            if state()["nodes"]["big"]["state"] == "free":
                break
        check("big -> free nach Ruhe", state()["nodes"]["big"]["state"] == "free")

        # big ist wieder free, granite (tier 2) noch warm, qwen kalt: warm zuerst -> granite; strikt -> qwen kalt
        st, txt = chat("standard:latest", ctx=16384)
        d = route()
        check("warm zuerst: warmes granite (tier 2) schlaegt kaltes qwen", d["node"] == "big" and d["model"] == "granite4.2:8b" and d["warm"] is True,
              f"{d} geladen={list(state()['nodes']['big']['loaded'])}")   # bei Fehlschlag sofort sichtbar, ob prewarm schon qwen geholt hat
        st, txt = chat("strikt:latest", ctx=16384)
        d = route()
        check("strikt (latency_first=false): tier0 qwen trotz Kaltstart", d["model"] == "qwen3-coder:30b" and d["tier"] == 0, str(d))
        for _ in range(10):   # ~7 s mit laufenden Heartbeats: prewarm on free (6 s) holt das Rang-1-Modell zurueck
            hb("big", 3, 8000, 24600); time.sleep(0.7)
        pre = [d for d in state()["decisions"] if d["event"] == "prewarm" and d.get("reason") == "free"]
        check("prewarm nach busy->free: Rang-1 wieder vorgeladen", any(p["node"] == "big" for p in pre), str(pre[-2:]))
        st, txt = chat("standard:latest", ctx=16384)
        d = route()
        check("standard nach prewarm -> tier0 qwen warm", d["node"] == "big" and d["tier"] == 0 and d["warm"] is True, str(d))

        # Residenz: ein konkret angefordertes Fremdmodell (gpt-oss, in keiner Rolle Rang 1) verdraengt qwen (Fake-Ollama
        # wirft bei >31.8 GiB das aelteste raus: granite, dann qwen). Ohne die Regel bliebe qwen kalt und "warm zuerst"
        # naehme fortan die Ausweichstufe; mit ihr kommt qwen nach residency_idle_s (3 s im Test) zurueck - aber nur,
        # weil der Verdraenger seit dann nicht mehr angefragt wurde.
        # Modellwechsel: waehrend gpt-oss laedt (slow_load 2.5 s, qwen zaehlt bis zum naechsten Poll als geladen), darf eine
        # zweite Anfrage auf dasselbe Modell nicht mit 503 'no node' abgewiesen werden, sondern wartet auf das Laden.
        hb("big", 2, 26600, 6000)   # 5.9 GiB frei: ohne qwen in /api/ps reicht das Budget fuer gpt-oss (12.8) nicht -> frueher 503
        first = {}
        th = threading.Thread(target=lambda: first.update(zip(("st", "txt"), chat("gpt-oss:20b", ctx=8192, slow_load=6), strict=True)))
        th.start(); time.sleep(2.5)   # Poll hat die Verdraengung gesehen, gpt-oss noch nicht da
        st2, txt2 = chat("gpt-oss:20b", ctx=8192)
        th.join()
        waits = [d for d in state()["decisions"] if d.get("event") == "wait_load"]
        check("Modellwechsel: zweite Anfrage wartet auf das Laden statt 503", first.get("st") == 200 and st2 == 200 and waits and waits[-1]["model"] == "gpt-oss:20b",
              f"erste {first.get('st')} zweite {st2} {txt2[:80] if st2 != 200 else ''} wait_load {len(waits)}")
        st, txt = chat("gpt-oss:20b", ctx=8192)
        time.sleep(0.8)   # der Kaltstart stoesst einen sofortigen Poll an; kurz warten, bis /api/ps eingelesen ist
        n = state()["nodes"]["big"]
        check("konkretes Fremdmodell verdraengt qwen (Vorbedingung)", st == 200 and "gpt-oss:20b" in n["loaded"] and "qwen3.6:35b-a3b" not in n["loaded"],
              f"geladen {list(n['loaded'])}")
        for _ in range(12):   # Heartbeats halten den Knoten free/gpu_known; Regel prueft jede Sekunde, Leerlauf 3 s
            hb("big", 3, 18000, 14600); time.sleep(0.6)   # 17.6 GiB belegt: gpt-oss 12 + Desktop, kein fremdes VRAM
            if state()["nodes"]["big"]["loaded"].get("qwen3.6:35b-a3b"):
                break
        n = state()["nodes"]["big"]
        res = [p for p in state()["decisions"] if p["event"] == "prewarm" and p.get("reason") == "residency"]
        check("Residenz: Rang-1 nach Leerlauf des Verdraengers wieder vorgewaermt", "qwen3.6:35b-a3b" in n["loaded"] and any(p["node"] == "big" for p in res),
              f"geladen {list(n['loaded'])}, prewarm-residency {len(res)}")
        st, txt = chat("standard:latest", ctx=16384)
        d = route()
        check("... und standard laeuft wieder warm auf tier0", d["node"] == "big" and d["tier"] == 0 and d["warm"] is True, str(d))

        # Warm-Fit: ein schon geladenes Modell braucht kein weiteres VRAM. Der Heartbeat direkt nach einer Antwort
        # zeigt wenig freies VRAM (Rechenpuffer); mit Budgetpruefung (1.27 + 20.6 - 1 = 20.9 < 21.5 GiB) fiele der
        # warme Knoten durch und die Anfrage ginge auf die Ausweichstufe - so lief am 2026-09-10 die zweite
        # Sprachrunde auf gpu-laptop/gemma statt auf dem warmen qwen.
        hb("big", 2, 31300, 1300)
        st, txt = chat("standard:latest", ctx=16384)   # qwen ist mit 16384 geladen (letzte Anfrage), also warm fuer <= 16384
        d = route()
        check("Warm-Fit: geladenes Modell wird trotz knappem freiem VRAM genommen", st == 200 and d["node"] == "big" and d["tier"] == 0 and d["warm"] is True, str(d))

        # --- Stufe 1: Request-Semantik (design/roadmap.md). Der Client beschreibt (routing-Block, Anforderungen aus dem
        # Request), der Router entscheidet und sagt in der Antwort, was er getan hat. ---
        hb("big", 3, 8000, 24600); time.sleep(0.3)   # wieder normal viel freies VRAM
        msg = [{"role": "user", "content": "hi"}]
        body = {"model": "standard:latest", "messages": msg, "stream": False,
                "routing": {"min_context": 40000, "request_id": "req-42", "session_id": "sess-1"}}
        REQ_T = time.time(); st, raw = http(R + "/api/chat", body)
        h1 = dict(LAST_HEADERS)   # vor route()/state(), die ueberschreiben LAST_HEADERS
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
        # abgeleitete Anforderungen: tools im Request, gpt-oss kann (im Fake) keine -> konkretes Modell 400
        tool = [{"type": "function", "function": {"name": "f", "description": "x", "parameters": {"type": "object", "properties": {}}}}]
        st, raw = http(R + "/api/chat", {"model": "gpt-oss:20b", "messages": msg, "stream": False, "tools": tool})
        check("konkretes Modell ohne Faehigkeit tools -> 400 mit Klartext", st == 400 and "tools" in raw.decode(), f"{st} {raw[:120]!r}")
        # Tool-Call im falschen Dialekt: Ollama verwirft ihn still (leerer content, keine tool_calls).
        # Die Rettungsstufe holt ihn zurueck - gemessen 2026-09-15 an qwen3-coder:30b, 80 % Verlust ab ~12k Token.
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
        # Gegenprobe: mit format ist die Stufe aus - eine JSON-Antwort ist dort das gewollte Ergebnis
        st, raw = http(R + "/api/chat", {"model": "qwen3.6:35b-a3b", "messages": msg, "stream": False,
                                         "tools": tool_t, "format": {"type": "object"}, "broken_tool_call": True})
        jr = json.loads(raw) if st == 200 else {}
        check("mit format keine Rettung (Text bleibt Text)",
              st == 200 and not (jr.get("message") or {}).get("tool_calls")
              and "<function=" in ((jr.get("message") or {}).get("content") or ""),
              f"{st} {raw[:120]!r}")
        # Rolle: alle Stufen fallen weg -> 400 (Fehler des Requests), nicht 503 (Kapazitaet)
        st, raw = http(R + "/api/chat", {"model": "strikt:latest", "messages": msg, "stream": False, "routing": {"require": ["vision"]}})
        check("routing.require vision: keine Stufe der Rolle kann es -> 400", st == 400 and "vision" in raw.decode(), f"{st} {raw[:120]!r}")
        # Rolle strikt (coder -> granite) mit think: coder denkt nicht (Fake) -> Stufe 0 uebersprungen, granite@16384 bedient
        st, raw = http(R + "/api/chat", {"model": "strikt:latest", "messages": msg, "stream": False, "think": True, "routing": {}})
        ri = (json.loads(raw) if st == 200 else {}).get("routing") or {}
        check("think in Rolle strikt: coder-Stufe uebersprungen (fehlende Faehigkeit thinking), granite bedient",
              st == 200 and ri.get("model") == "granite4.2:8b" and ri.get("tier") == 1 and len(ri.get("skipped", [])) == 1
              and "thinking" in ri["skipped"][0]["reason"] and ri.get("required") == ["thinking"], f"{st} {ri}")
        # granite wieder von big entladen, damit der Unload-Test unten nur qwen vorfindet
        http("https://127.0.0.1:21001/api/generate", {"model": "granite4.2:8b", "keep_alive": 0}, headers={"X-Router-Token": "testtoken"}, auth=False)
        time.sleep(1.2)
        # Katalog-Override: gpt-oss structured:false -> format-Schema wird abgelehnt, bevor Ollama 500 liefert
        st, raw = http(R + "/api/chat", {"model": "gpt-oss:20b", "messages": msg, "stream": False, "format": {"type": "object"}})
        check("Katalog-Override capabilities.structured=false -> 400 bei format-Schema", st == 400 and "structured" in raw.decode(), f"{st} {raw[:120]!r}")
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {"require": ["telepathie"]}})
        check("unbekannte Faehigkeit im routing-Block -> 400", st == 400 and "telepathie" in raw.decode(), f"{st} {raw[:120]!r}")
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {"foo": 1}})
        check("unbekanntes Feld im routing-Block -> 400", st == 400 and "foo" in raw.decode(), f"{st} {raw[:120]!r}")
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "stream": False,
                                         "messages": [{"role": "user", "content": "hi", "images": ["AA==", "AA==", "AA=="]}]})
        check("Limit max_images 2: drei Bilder -> 413", st == 413 and "images" in raw.decode(), f"{st} {raw[:120]!r}")
        # /v1: derselbe routing-Block, Info im Objekt und Header
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

        # --- Stufe 3: Scheduler - Score, Statistik, Circuit Breaker, Admission Control mit Prioritaeten ---
        st, raw = http(R + "/api/chat", {"model": "standard:latest", "messages": msg, "stream": False, "routing": {}})
        ri = (json.loads(raw) if st == 200 else {}).get("routing") or {}
        cands = ri.get("candidates") or []
        check("Score: Kandidaten der Stufe mit Punkten in der Routing-Info, gewaehlt = hoechster (warm)", st == 200 and cands
              and cands[0]["node"] == ri.get("node") and cands[0]["warm"] is True and cands[0]["score"] > 100, str(cands))
        pe = state()["perf"].get("qwen3.6:35b-a3b@big") or {}
        check("Statistik: EWMA tok/s und Ergebniszaehler je Modell@Knoten", (pe.get("ewma") or {}).get("gen_tps") and (pe.get("outcomes") or {}).get("ok", 0) >= 1
              and ri.get("priority") == "normal", str({k: pe.get(k) for k in ("ewma", "outcomes")}))
        # Circuit Breaker: drei Backend-Fehler (500 vor dem ersten Byte) -> big OPEN (2 s im Test); Anfragen gehen auf small,
        # obwohl qwen auf big warm ist; nach open_s eine Probe (half_open) -> Erfolg -> closed
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
        # Admission: big max_inflight 1 (Policy). Eine lange Anfrage laeuft (1.5 s); batch und interactive warten;
        # interactive wird zuerst zugelassen. deadline_ms 300 -> 503 statt warten. Danach Policy zurueck.
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

        # --- Stufe 4: Observability - /metrics (Prometheus-Text) und Usage ---
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

        # --- Stufe 6: Betrieb - Config-Schema, Idempotency, Canary, Shadow, Supply Chain ---
        router_py = os.path.join(HERE, "..", "router", "router.py")
        rc = subprocess.run([PY, router_py, "--check", os.path.join(HERE, "test-config.yaml"), "--pure"], capture_output=True, text=True, cwd=HERE)
        check("router.py --check: Testkonfiguration gueltig (rc 0)", rc.returncode == 0 and "Konfiguration ok" in rc.stdout, (rc.stdout + rc.stderr)[-200:])
        bad = os.path.join(HERE, "bad-config.yaml")
        open(bad, "w", encoding="utf-8").write(open(os.path.join(HERE, "test-config.yaml"), encoding="utf-8").read().replace("  limits: {", "  limitz: {"))
        rc = subprocess.run([PY, router_py, "--check", bad, "--pure"], capture_output=True, text=True, cwd=HERE)
        os.remove(bad)
        check("--check: Tippfehler router.limitz -> rc 2, benannt, mit Vorschlag 'limits'", rc.returncode == 2 and "router.limitz" in rc.stdout and "'limits'" in rc.stdout, (rc.stdout + rc.stderr)[-220:])
        # Idempotency: Wiederholung mit demselben Schluessel = dieselbe Antwort, keine zweite Inferenz
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
        # Canary 100 %: Rolle kanarie (granite) laeuft auf der Canary-Stufe qwen; Shadow 100 %: danach granite@4096 im Schatten
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

        # --- Stufe 5: Cloud als Stufe in Rollen (lokal zuerst; Datenklasse, Credential-Scan, Opt-out, Budget) ---
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

        # --- Stufe 7: Einstellungen per UI (settings-Block in roles.yaml, Allowlist settings.py), Cloud-Modelle im Katalog, Logo ---
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
        check("nicht freigegebener Pfad (base_url) -> 400", st == 400 and "nicht ueber die UI" in raw.decode(), raw.decode()[:120])
        st, raw = http(C + "/admin/config", {"settings": {"modes.admission.max_inflight_default": 0}}, method="PUT")
        check("Wert ausserhalb des Bereichs -> 400", st == 400 and "zwischen" in raw.decode(), raw.decode()[:120])
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
        check("--check akzeptiert roles.yaml mit settings (Deploy-Pruefung auf dem CT)", rc7.returncode == 0 and "mit roles.yaml" in rc7.stdout, (rc7.stdout + rc7.stderr)[-200:])
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
        check("UI-Modell in einer Rolle -> Loeschen abgelehnt", st == 200 and st2 == 400 and "Rollen" in raw2.decode(), f"{st} {raw.decode()[:80]} {st2} {raw2.decode()[:100]}")
        roles7["wolke"]["tiers"].pop()
        http(C + "/admin/config", {"roles": roles7}, method="PUT")
        st2, raw2 = http(C + "/admin/config", {"delete_models": ["testcloud:maxi", "testcloud:mini"]}, method="PUT")
        m7 = json.loads(http(C + "/admin/config")[1])["models"]
        check("UI-Modell geloescht, Katalogeintrag aus config.yaml bleibt", st2 == 200 and "testcloud:maxi" not in m7 and "testcloud:mini" in m7, raw2.decode()[:100])
        st, raw = http(C + "/skirnir.png")
        check("Logo /skirnir.png (PNG), Tab-Icon /favicon.png und /favicon.ico", st == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n"
              and http(C + "/favicon.png")[1][:4] == b"\x89PNG" and http(C + "/favicon.ico")[0] == 200)

        # --- Clients-Tab: Client anlegen, Secret erzeugen/rotieren, Quell-IP, loeschen (alles in roles.yaml, config.yaml bleibt) ---
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
                                                 "router.cloud.providers.testcloud.budget_month_chf": 50}}, method="PUT")   # Budget: der Cloud-Block oben hat 1.9 CHF verbraucht
        ri = rinfo(*http(R + "/api/chat", {"model": "wolke:latest", "messages": msg, "stream": False, "routing": {"execution": "cloud"}})[::-1])
        check("Quell-IP + Datenklasse per UI: Anfrage ohne Header ist pi-voice und darf in die Cloud", st == 200 and ri.get("client") == "pi-voice" and ri.get("node") == "cloud:testcloud",
              f"{st} {raw.decode()[:80]} client={ri.get('client')} node={ri.get('node')} {ri.get('skipped')}")
        http(C + "/admin/config", {"settings": {"router.cloud.providers.testcloud.budget_month_chf": None}}, method="PUT")
        st, raw = http(C + "/admin/config", {"settings": {"router.client_auth.clients.pi-voice.ip": "nix"}}, method="PUT")
        check("ungueltige IP -> 400", st == 400 and "IP-Adresse" in raw.decode(), raw.decode()[:100])
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

        # --- Decision Engine (2026-09-16): Auto-Rolle ueber Kette Fake-Jevlike -> Regeln ---
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

        # Unload-Nachlauf: /api/ps meldet sofort nichts mehr, nvidia-smi zeigt den Speicher noch. Ohne
        # vram_settle_s erschiene der freiwerdende Speicher als fremdes VRAM (26600 MiB belegt - 0 Ollama
        # - 7 GiB Baseline = 19 GiB "fremd") -> Knoten faelschlich busy und prewarm blockiert.
        vor = state()["nodes"]["big"]
        http("https://127.0.0.1:21001/api/generate", {"model": "qwen3.6:35b-a3b", "keep_alive": 0},
             headers={"X-Router-Token": "testtoken"}, auth=False)
        n = vor
        for _ in range(8):   # bis der Poll das leere /api/ps gesehen hat; Heartbeat meldet weiter viel belegtes VRAM
            hb("big", 2, 26600, 6000); time.sleep(0.5)
            n = state()["nodes"]["big"]
            if not n["loaded"]:
                break
        check("Unload-Nachlauf: freiwerdendes VRAM gilt nicht als fremd", vor["loaded"] and not n["loaded"]
              and n["foreign_vram_gib"] == 0 and n["state"] == "free",
              f"geladen {list(vor['loaded'])} -> {list(n['loaded'])}, fremd {n['foreign_vram_gib']} GiB, {n['state']}")

        # Lade-Nachlauf (Gegenrichtung): waehrend Ollama laedt, sieht nvidia-smi das Modell schon, /api/ps noch
        # nicht. Ohne announce_load gilt das ladende Modell als fremdes VRAM - und ein Kaltstart, der laenger
        # dauert als foreign_sustain_s, macht den Knoten mitten im Laden busy; das Sicherheitsnetz entlaedt dann
        # genau das eben geladene Modell. `slow_load` haelt das Fake-Ollama 3 s in genau diesem Zustand.
        # Erst den Nachlauf des Unloads auslaufen lassen (vram_settle_s = 8 s), sonst deckt DER schon das Fenster
        # ab und der Test wuerde auch ohne announce_load bestehen (genau so passiert, bevor diese Schleife da war).
        for _ in range(7):
            hb("big", 3, 8000, 24600); time.sleep(1.5)   # wenig belegt: kein fremdes VRAM, Knoten bleibt free

        # Baseline-Lernregel (nichts geladen, also lernt der Router gerade): der Median der Stundenmittel, NICHT
        # das Minimum. Ein einzelner sehr ruhiger Moment - Monitor aus - darf den Grundverbrauch nicht dauerhaft
        # nach unten ziehen; genau daran ging gpu-desktop grundlos busy.
        def gelernt():
            r = next(n for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")
            return (r.get("learned") or {}).get("baseline_gib")
        hb("big", 3, 2000, 30000)      # Ausreisser nach unten: 1.95 GiB belegt
        for _ in range(6):
            hb("big", 3, 9000, 23000); time.sleep(0.3)   # Normalbetrieb: 8.79 GiB
        b1 = gelernt()
        check("Baseline lernt den Normalbetrieb, nicht den ruhigsten Moment", b1 is not None and b1 > 4.0,
              f"gelernt {b1} GiB (Ausreisser 1.95, Normalfall 8.79 - das alte Minimum haette 1.95 geliefert)")
        # Spielstunden gehoeren nicht hinein: bei hoher Auslastung wird gar nicht erst gesammelt
        for _ in range(3):
            hb("big", 80, 30000, 2000); time.sleep(0.2)   # "Spiel": 29.3 GiB belegt, GPU heiss
        b2 = gelernt()
        check("bei heisser GPU wird nicht gelernt (ein Spiel faelscht die Baseline nicht)", b2 == b1,
              f"{b1} -> {b2} GiB")
        hb("big", 3, 8000, 24600)   # wieder Ruhe fuer den naechsten Test
        res = {}

        def lade():
            res["st"], _ = http(R + "/api/chat", {"model": "standard:latest", "stream": False, "slow_load": 3,
                                                  "messages": [{"role": "user", "content": "hi"}],
                                                  "options": {"num_ctx": 16384}}, timeout=30)
        th = threading.Thread(target=lade, daemon=True)
        th.start()
        time.sleep(1.0)             # mitten im Ladevorgang
        hb("big", 2, 26600, 6000)   # nvidia-smi sieht das Modell bereits: 26000 MiB belegt
        n = state()["nodes"]["big"]
        # seit 2026-09-11 zaehlt waehrend des Ladens mindestens das fremde VRAM von VOR dem Laden weiter (hier 0,8 GiB aus
        # 7,8 belegt - 7 Baseline); nur das ladende Modell selbst darf nicht als fremd erscheinen
        check("Lade-Nachlauf: ladendes Modell gilt nicht als fremdes VRAM", not n["loaded"]
              and n["foreign_vram_gib"] <= 0.9 and n["state"] == "free",
              f"geladen {list(n['loaded'])}, fremd {n['foreign_vram_gib']} GiB, {n['state']}")
        th.join(timeout=25)
        check("... und die Anfrage laeuft normal durch", res.get("st") == 200, str(res.get("st")))

        # Busy-Schwellen je Knoten (Policy): big mit busy_foreign_gib 0.5 -> 1 GiB fremd macht busy, obwohl global 3.0 gilt
        fpb = next(n["fp"] for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")
        st, raw = http(C + f"/admin/nodes/{fpb}/policy", {"busy_foreign_gib": 0.5, "busy_gpu_util_pct": 90})
        for _ in range(12):   # Policy-Aenderung schickt dem Agenten ein Konfigurationspaket, der Tunnel kommt kurz neu
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

        # Tunnel weg (Agent stirbt) -> big sofort offline; Agent zurueck -> big wieder free
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
        check("Tunnel-Ereignisse protokolliert", [d["state"] for d in state()["decisions"] if d["event"] == "tunnel"][-2:] == ["down", "up"])
        st, txt = chat("standard:latest", ctx=16384)
        check("Anfrage durch den neuen Tunnel", st == 200, f"{st} {txt[:60]}")
        # Knoten big stirbt (Ollama weg, Tunnel bleibt) -> offline -> WOL-Versuch -> 503 nach wait_up_s
        procs[0].terminate(); procs[0].wait()
        for _ in range(20):   # 2 Misses x (1 s Poll + Connect-Fehler), Windows braucht ~2 s pro Refused-Connect
            if state()["nodes"]["big"]["state"] == "offline": break
            time.sleep(0.5)
        check("big offline erkannt", state()["nodes"]["big"]["state"] == "offline")
        t0 = time.time()
        st, txt = chat("gross:latest")
        dt = time.time() - t0
        ev = [d["event"] for d in state()["decisions"][-5:]]
        check("gross ohne Knoten: WOL versucht, dann 503", st == 503 and "wol" in ev and 3.5 <= dt <= 8, f"{st} {dt:.1f}s {ev}")
        st, txt = chat("standard:latest", ctx=4096)
        d = route()
        check("standard 4096 -> small (granite tier2, 4.95+0.65+0.8=6.4 <= 7)", st == 200 and d["node"] == "small", str(d))

        # Cooldown: zweiter gross-Call darf NICHT wieder 4 s warten
        t0 = time.time(); st, txt = chat("gross:latest"); dt = time.time() - t0
        check("WOL-Cooldown: sofort 503", st == 503 and dt < 1.5, f"{dt:.1f}s")

        # konkrete Modelle: vorhanden -> geroutet, unbekannt -> 404 wie Ollama
        st, txt = chat("granite4.2:8b", ctx=4096)
        d = route()
        check("konkretes Modell wird 1:1 geroutet", st == 200 and d["model"] == "granite4.2:8b" and json.loads(txt.splitlines()[0])["model"] == "granite4.2:8b", str(d))
        st, txt = chat("gibtsnicht:7b")
        check("unbekanntes konkretes Modell -> 404", st == 404 and "not found" in txt, f"{st} {txt[:60]}")
        # Basic Auth auf dem Control-Port
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

        # OpenAI-kompatibel (/v1): Node-RED-MCP spricht chat/completions ohne Streaming, mit tools/response_format
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
        t_oa = time.time()   # kein chat() -> Zeitpunkt fuer route() selbst merken
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
        # Tool-Ergebnis zurueck: assistant.tool_calls (arguments String) + role tool -> nativ (arguments Objekt)
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
        st, raw = http(R + "/v1/files")
        check("/v1/<unbekannt> -> 404 OpenAI-Fehler", st == 404 and "not supported" in json.loads(raw)["error"]["message"], raw.decode()[:100])
        st, raw = http(C + "/admin/try", {"model": "klein:latest", "prompt": "hi"})
        j = json.loads(raw)
        check("/admin/try liefert Entscheidung + Antwort", st == 200 and j["status"] == 200 and j["decision"]["model"] == "granite4.2:8b" and "Hallo" in j["answer"], raw.decode()[:160])
        check("roles.yaml geschrieben", os.path.exists(os.path.join(HERE, "roles.yaml")))
        # Messung: granite auf small (big ist offline) -> weights 4.95, kv 0.158 aus dem Fake
        st, raw = http(C + "/admin/measure", {"model": "granite4.2:8b"})
        check("/admin/measure startet auf small", st == 200 and json.loads(raw)["node"] == "small", raw.decode()[:100])
        for _ in range(40):
            if not state()["measuring"]: break
            time.sleep(0.5)
        cfg = json.loads(http(C + "/admin/config")[1])
        g = cfg["models"]["granite4.2:8b"]
        check("Messung: weights/kv aus /api/ps abgeleitet, Quelle gemessen", g.get("source") == "gemessen" and abs(g["kv_gib_per_1k"] - 0.158) < 0.01 and abs(g["weights_gib"] - 4.95) < 0.05, str(g))
        st, raw = http(C + "/admin/measure", {"model": "qwen3.6:35b-a3b"})
        check("Messung ohne freien Knoten mit dem Modell -> 409", st == 409, raw.decode()[:100])
        # Leistung: passiv aus den bisherigen Anfragen (Fake: 30 Token / 0.5 s = 60 tok/s)
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
        # roles_total 9 = 8 aus der Konfiguration (inkl. kanarie, wolke, wolke2) + klein aus der UI
        check("/admin/ha: Knoten/Modelle/Rollen aggregiert", st == 200 and ha["nodes_total"] == 2 and ha["nodes_online"] == 1
              and "granite4.2:8b" in ha["models"] and ha["roles_total"] == 9, json.dumps({k: ha[k] for k in ("nodes_online", "models_available", "roles_ready", "problem", "problems")}))
        check("/admin/ha: gross ohne Knoten -> problem=true mit Grund", ha["problem"] is True and any("gross" in p for p in ha["problems"]) and ha["roles"]["gross"]["ready"] is False, str(ha["problems"]))
        check("/admin/ha: klein bereit auf small", ha["roles"]["klein"]["ready"] is True and ha["roles"]["klein"]["node"] == "small", str(ha["roles"]["klein"]))
        check("/admin/ha: Anfragen der letzten 5 min gezaehlt", ha["requests_5min"] >= 5 and ha["last_route"].startswith("klein ->"), f"{ha['requests_5min']} {ha['last_route']}")
        # Agent-Ausfall: small meldet einmal, dann Stille -> nach agent_missing_problem_s (8 s) ein HA-Problem
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
        # ---- Stufe 2: Client-Authentifizierung auf dem API-Port ------------------------------------------------
        def ca():
            return json.loads(http(C + "/admin/state")[1])["client_auth"]

        def hdr(tok):
            return {"Authorization": "Bearer " + tok}

        def chat_body(m):
            return {"model": m, "messages": [{"role": "user", "content": "hi"}], "stream": False}
        st, raw = http(R + "/api/tags")   # ohne Identitaet, nicht exempt
        v = ca()
        check("client_auth observe: Unbekannte werden bedient und gezaehlt (IP, Pfad)", st == 200 and v["mode"] == "observe"
              and any(u["total"] >= 1 and u["last_path"] for u in v["unauthenticated"].values()), json.dumps(v["unauthenticated"])[:160])
        st, raw = http(R + "/api/tags", headers=hdr("tester-token"))
        t = ca()["clients"]["tester"]
        check("Bearer-Token identifiziert den Client", st == 200 and t["total"] >= 1 and t["via"] == "bearer", json.dumps(t))
        st, raw = http(R + "/api/tags", headers={"Authorization": "Basic " + base64.b64encode(b"tester:tester-token").decode()})
        check("Basic <client>:<token> identifiziert ebenfalls", st == 200 and ca()["clients"]["tester"]["via"] == "basic")
        st, raw = http(R + "/api/tags", headers=hdr("voellig-falsch"))
        check("falsches Token zaehlt als bad_token (kein Rueckfall auf die IP)", st == 200
              and any(u["bad_token"] >= 1 for u in ca()["unauthenticated"].values()))
        st, raw = http(R + "/api/chat", chat_body("standard:latest"), headers=hdr("restricted-token"))
        check("Rolle fuer diesen Client nicht erlaubt -> 403", st == 403 and "not allowed" in raw.decode(), f"{st} {raw[:90]}")
        st, raw = http(R + "/api/chat", chat_body("klein:latest"), headers=hdr("restricted-token"))
        check("erlaubte Rolle -> 200", st == 200, f"{st} {raw[:60]}")
        # konkrete Modelle sind seit dem UI-Test oben (expose_concrete_models: False) verborgen -> fuer diese Pruefung
        # einschalten, sonst kaeme 404 statt 403 (genau so passiert), danach wieder aus
        http(C + "/admin/config", {"expose_concrete_models": True}, method="PUT")
        st, raw = http(R + "/api/chat", chat_body("granite4.2:8b"), headers=hdr("restricted-token"))
        nd = {k: (v["state"], sorted(v["models"])) for k, v in state()["nodes"].items()}   # Kontext fuer den Fehlerfall
        check("konkretes Modell fuer diesen Client gesperrt -> 403", st == 403 and "concrete model names" in raw.decode(),
              f"{st} {raw[:90]} | Knoten: {nd}")
        http(C + "/admin/config", {"expose_concrete_models": False}, method="PUT")
        codes = [http(R + "/api/tags", headers=hdr("limited-token"))[0] for _ in range(4)]
        check("Rate Limit 3/min: die vierte Anfrage bekommt 429", codes == [200, 200, 200, 429], str(codes))
        # client_auth.locked (Audit 2026-09-16): gesperrter Modus ist per Settings nicht aenderbar, Konfiguration traegt das Flag
        sys.path.insert(0, os.path.join(HERE, "..", "router"))
        from skirnir_router import settings as _smod   # noqa: E402
        from skirnir_router.config import Config as _Cfg   # noqa: E402
        locked_cfg = {"router": {"client_auth": {"mode": "enforce", "locked": True}}}
        try:
            _smod.coerce("router.client_auth.mode", "observe", locked_cfg); lock_err = ""
        except ValueError as e:
            lock_err = str(e)
        unlocked_ok = _smod.coerce("router.client_auth.mode", "observe", {"router": {"client_auth": {"mode": "enforce"}}}) == "observe"
        check("client_auth.locked: Settings lehnen den Modus ab, ungesperrt geht er durch", "gesperrt" in lock_err and unlocked_ok, lock_err[:80])
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

        # --- Agent-Update ueber den Router (agentupdate.py): Manifest signiert vom Betreiber-Schluessel, Auftrag per UI-Knopf,
        # Fake-Agent prueft Signatur/Hash, laedt mit Einmal-Token, meldet Stand im Heartbeat und meldet sich mit der neuen Version an.
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
        # Fehlerfall: Manifest mit fremdem Schluessel signiert -> Agent lehnt ab, HA-Problem
        _manifest("test3", _Ed.generate())
        st, raw = http(C + f"/admin/nodes/{fp_big}/update", {})
        for _ in range(40):
            u = reg_big()["update"]
            if u.get("state") in ("done", "failed") and u.get("version") == "test3": break
            time.sleep(0.25)
        ha_u = json.loads(http(C + "/admin/ha")[1])
        check("Agent-Update: falsche Signatur -> failed, HA-Problem, Version unveraendert, kein Drain", st == 200 and u.get("state") == "failed" and "Signatur" in (u.get("message") or "")
              and any("Agent-Update auf big fehlgeschlagen" in p for p in ha_u["problems"]) and reg_big()["facts"]["agent_version"] == "test2" and not state()["nodes"]["big"]["draining"], str(u) + str(ha_u["problems"]))
        st, raw = http(C + "/v1/agent/binary/skirnir-agent-test3-windows-amd64.exe", headers={"Authorization": "Bearer falsch"})
        check("Agent-Update: Download ohne gueltiges Token -> 403", st == 403, str(st))

        # Metriken persistent (0.1.9): metrics.json neben der Config, gesichert 60 s nach der ersten Aenderung (tick_loop); ein frischer
        # Prozess laedt Zaehler und Histogramme und liefert dieselben Werte in /metrics
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

        # Sperren: Knoten verschwindet aus dem Routing, Agent bekommt 'revoked'; danach loeschen
        fp = next(n["fp"] for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["name"] == "big")
        st, raw = http(C + f"/admin/nodes/{fp}/revoke", {})
        time.sleep(1.5)
        logs["agent-big"].seek(0); alog = logs["agent-big"].read()
        check("Sperren: big aus dem Routing, Register 'revoked', Agent informiert", st == 200 and "big" not in state()["nodes"]
              and next(n["state"] for n in json.loads(http(C + "/admin/nodes")[1])["nodes"] if n["fp"] == fp) == "revoked" and '"status": "revoked"' in alog, alog[-160:])
        # Agent zuerst beenden: er verbindet sich alle 0.5 s neu und wuerde den geloeschten Schluessel sofort
        # wieder als "wartet auf Freigabe" eintragen - daran ist die Pruefung sporadisch gescheitert.
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
