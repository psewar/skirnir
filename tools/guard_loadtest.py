"""Lasttest GPU-Schutz (design/gpu-guard.md, Test 6): je Power-Limit 575/500/460/400 W Tempo (Prefill, Generierung, 2 parallel),
16-Pin-Spannung, Leistung und Throttle-Gruende messen. Laeuft ERHOEHT (nvidia-smi -pl braucht Admin); der Guard steht waehrenddessen
auf 100 % (guard_override.py). Am Ende: Limit auf den Standard, dann stellt der Guard sein Dauerlimit selbst wieder her.
Aufruf: python guard_loadtest.py <ops-ordner> <ausgabe.json>"""
import base64, json, os, random, ssl, statistics, subprocess, sys, threading, time, urllib.request

OPS, OUT = sys.argv[1], sys.argv[2]
sys.path.insert(0, r"D:\repos\skirnir\deploy")
os.environ["SKIRNIR_OPS"] = OPS
import ops_env  # noqa: E402

ops = ops_env.load(); ops_env.load_secrets(ops)
AUTH = "Basic " + base64.b64encode(f"{ops['UI_USER']}:{os.environ[ops['UI_PASS_ENV']]}".encode()).decode()
ROUTER = f"https://{ops['ROUTER_HOST']}:11435"
CTX = ssl.create_default_context()
MODEL = sys.argv[3] if len(sys.argv) > 3 else "qwen3.6:35b-a3b"
LEVELS = [int(x) for x in sys.argv[4].split(",")] if len(sys.argv) > 4 else [575, 500, 460, 400]
LOG = []


def say(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True); LOG.append(line)


def router(path, body=None, timeout=900):
    req = urllib.request.Request(ROUTER + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    return json.load(urllib.request.urlopen(req, timeout=timeout, context=CTX))


def health():
    return json.load(urllib.request.urlopen("http://127.0.0.1:10398/health", timeout=5))


def smi(*args):
    return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True).stdout.strip()


def set_limit(w):
    out = subprocess.run(["nvidia-smi", "-pl", str(w)], capture_output=True, text=True)
    cur = smi("--query-gpu=power.limit", "--format=csv,noheader,nounits")
    say(f"nvidia-smi -pl {w}: {out.stdout.strip() or out.stderr.strip()} -> aktuell {cur} W")
    return abs(float(cur) - w) < 1


class Sampler(threading.Thread):
    """/health jede Sekunde: Leistung, 16-Pin, Spannung, Throttle, Temperaturen."""
    def __init__(self):
        super().__init__(daemon=True); self.rows = []; self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                s = health()["gpu"]["sensors"]
                self.rows.append({k: s.get(k) for k in ("power_w", "pin16_power_w", "pin16_voltage_v", "throttle_reasons", "temp_c", "mem_temp_c", "hotspot_c", "gpu_voltage_v", "fan_pct")})
            except Exception as e:  # noqa: BLE001
                self.rows.append({"err": str(e)[:60]})
            time.sleep(1)

    def summary(self):
        r = [x for x in self.rows if "err" not in x and x.get("power_w") is not None]
        if not r:
            return {}
        pw = [x["power_w"] for x in r]; p16 = [x["pin16_power_w"] for x in r if x.get("pin16_power_w") is not None]
        v = [x["pin16_voltage_v"] for x in r if x.get("pin16_voltage_v") is not None]
        loaded = [x for x in r if (x.get("pin16_power_w") or x["power_w"]) >= 300]
        vload = [x["pin16_voltage_v"] for x in loaded if x.get("pin16_voltage_v") is not None]
        thr = {}
        for x in r:
            for t in x.get("throttle_reasons") or []:
                thr[t] = thr.get(t, 0) + 1
        return {"samples": len(r), "power_w_max": max(pw), "power_w_p95": statistics.quantiles(pw, n=20)[18] if len(pw) >= 20 else max(pw),
                "pin16_w_max": max(p16) if p16 else None, "pin16_v_min": min(v) if v else None,
                "pin16_v_under_load_median": statistics.median(vload) if vload else None, "pin16_v_under_load_min": min(vload) if vload else None,
                "samples_over_300w": len(loaded), "throttle_counts": thr, "temp_max": max(x["temp_c"] for x in r if x.get("temp_c") is not None),
                "mem_temp_max": max((x["mem_temp_c"] for x in r if x.get("mem_temp_c") is not None), default=None),
                "hotspot_max": max((x["hotspot_c"] for x in r if x.get("hotspot_c") is not None), default=None)}


WORDS = ("Router Modell Knoten Stecker Leistung Spannung Speicher Kontext Anfrage Antwort Messung Stufe Grenze Zeit Wert Karte "
         "Dienst Agent Tunnel Zustand Warnung Ereignis Referenz Abfall Dauer Limit Standard Prozent Sekunde Minute").split()


def big_prompt(n_words):
    rnd = random.Random()
    body = " ".join(rnd.choice(WORDS) for _ in range(n_words))
    return f"Kennung {rnd.randint(10**9, 10**10)}. Lies den folgenden Text und antworte danach nur mit OK.\n\n{body}\n\nAntworte nur mit OK."


def try_req(prompt, tag):
    t0 = time.time()
    r = router("/admin/try", {"model": MODEL, "prompt": prompt, "think": False})
    r["wall_s"] = round(time.time() - t0, 1); r["tag"] = tag
    p = r.get("perf") or {}
    say(f"  {tag}: status {r.get('status')} wall {r['wall_s']} s prompt_tok {p.get('prompt_tokens')} prompt_tps {p.get('prompt_tps')} gen_tok {p.get('tokens')} gen_tps {p.get('gen_tps')} ttft {p.get('ttft_ms')} ms queued {r.get('queued_ms')} ms")
    return r


def level_run(w):
    res = {"limit_w": w}
    if not set_limit(w):
        res["error"] = "Limit nicht gesetzt"; return res
    time.sleep(3)
    smp = Sampler(); smp.start()
    # a) Prefill: langer Prompt (frische Kennung -> kein Prompt-Cache)
    res["prefill"] = [try_req(big_prompt(14000), "prefill") for _ in range(2)]
    # b) Generierung: 2 parallel, laengere Antwort
    gen_prompt = lambda: f"Kennung {random.randint(10**9, 10**10)}. Schreibe eine Geschichte von etwa 400 Woertern ueber einen Boten, der in ein fremdes Reich reitet."  # noqa: E731
    out = [None, None]
    ths = [threading.Thread(target=lambda i=i: out.__setitem__(i, try_req(gen_prompt(), f"gen{i}"))) for i in range(2)]
    [t.start() for t in ths]; [t.join() for t in ths]
    res["gen_parallel"] = out
    # c) Prefill + Generierung parallel (Spitze)
    out2 = [None, None]
    ths = [threading.Thread(target=lambda: out2.__setitem__(0, try_req(big_prompt(14000), "mix-prefill"))),
           threading.Thread(target=lambda: out2.__setitem__(1, try_req(gen_prompt(), "mix-gen")))]
    [t.start() for t in ths]; [t.join() for t in ths]
    res["mix"] = out2
    time.sleep(2); smp.stop.set(); smp.join(timeout=3)
    res["sensors"] = smp.summary()
    say(f"  Sensoren @{w} W: {json.dumps(res['sensors'])}")
    return res


def main():
    say("Start Lasttest; Guard:", json.dumps({k: health()["gpu_guard"].get(k) for k in ("state", "limit_w", "target_w")}))
    say("Limits:", smi("--query-gpu=power.limit,power.default_limit,power.min_limit,power.max_limit", "--format=csv,noheader"))
    results = {"model": MODEL, "levels": [], "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        say("Warmlauf"); try_req("Kennung 1. Antworte nur mit OK.", "warm")
        for w in LEVELS:
            say(f"=== Limit {w} W ===")
            results["levels"].append(level_run(w))
            time.sleep(10)   # Abkuehlen zwischen den Stufen
    finally:
        set_limit(575)   # Standard; das Dauerlimit stellt der Guard nach dem Zuruecksetzen der Config selbst her
        results["log"] = LOG
        json.dump(results, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        say("Ergebnis:", OUT)


main()
