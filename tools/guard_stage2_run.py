"""Nachtlauf Stufe 2 (design/gpu-guard.md, Zustandsfolge): Dauer-Vollast, bis der Guard 'gedrosselt' meldet, dann Last weg und
Erholung beobachten. Kein Admin noetig - der Guard setzt die Limits selbst. Zeichnet /health alle 2 s auf.
Aufruf: python guard_stage2_run.py <ops-ordner> <ausgabe.json> [modell] [worker]"""
import base64, json, os, random, ssl, sys, threading, time, urllib.request

OPS, OUT = sys.argv[1], sys.argv[2]
MODEL = sys.argv[3] if len(sys.argv) > 3 else "gemma4:26b"
WORKERS = int(sys.argv[4]) if len(sys.argv) > 4 else 4
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy"))
os.environ["SKIRNIR_OPS"] = OPS
import ops_env  # noqa: E402

ops = ops_env.load(); ops_env.load_secrets(ops)
AUTH = "Basic " + base64.b64encode(f"{ops['UI_USER']}:{os.environ[ops['UI_PASS_ENV']]}".encode()).decode()
ROUTER = f"https://{ops['ROUTER_HOST']}:11435"
CTX = ssl.create_default_context()
MAX_LOAD_S = 16 * 60      # Notbremse: laenger wird nicht belastet
RECOVER_MAX_S = 9 * 60
LOG, ROWS, REQS = [], [], []
stop_load = threading.Event()
lock = threading.Lock()


def say(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True); LOG.append(line)


def router(path, body=None, timeout=900):
    req = urllib.request.Request(ROUTER + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    return json.load(urllib.request.urlopen(req, timeout=timeout, context=CTX))


def health():
    return json.load(urllib.request.urlopen("http://127.0.0.1:10398/health", timeout=5))


WORDS = ("Router Modell Knoten Stecker Leistung Spannung Speicher Kontext Anfrage Antwort Messung Stufe Grenze Zeit Wert Karte "
         "Dienst Agent Tunnel Zustand Warnung Ereignis Referenz Abfall Dauer Limit Standard Prozent Sekunde Minute").split()


def big_prompt(n_words):
    rnd = random.Random()
    return f"Kennung {rnd.randint(10**9, 10**10)}. Lies den Text und antworte nur mit OK.\n\n" + " ".join(rnd.choice(WORDS) for _ in range(n_words)) + "\n\nAntworte nur mit OK."


def worker(i):
    while not stop_load.is_set():
        t0 = time.time()
        try:
            r = router("/admin/try", {"model": MODEL, "prompt": big_prompt(14000), "think": False})
            p = r.get("perf") or {}
            with lock:
                REQS.append({"t": t0, "wall": round(time.time() - t0, 2), "status": r.get("status"), "prompt_tps": p.get("prompt_tps"), "queued_ms": r.get("queued_ms")})
        except Exception as e:  # noqa: BLE001
            with lock:
                REQS.append({"t": t0, "wall": round(time.time() - t0, 2), "status": 599, "error": str(e)[:80]})
            time.sleep(2)


def sample():
    h = health(); g = h["gpu_guard"]; s = h["gpu"].get("sensors") or {}
    row = {"t": time.time(), "state": g.get("state"), "limit_w": g.get("limit_w"), "target_w": g.get("target_w"), "hochlast_s": g.get("hochlast_s"),
           "power_w": s.get("power_w"), "pin16_w": s.get("pin16_power_w"), "pin16_v": s.get("pin16_voltage_v"), "throttle": s.get("throttle_reasons"),
           "temp": s.get("temp_c"), "mem_temp": s.get("mem_temp_c"), "hotspot": s.get("hotspot_c"), "warnungen": g.get("warnungen")}
    ROWS.append(row)
    return row


def main():
    say(f"Nachtlauf Stufe 2: Modell {MODEL}, {WORKERS} Worker; Guard:", json.dumps({k: health()["gpu_guard"].get(k) for k in ("state", "limit_w", "target_w")}))
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(WORKERS)]
    [t.start() for t in threads]
    t_start = time.time(); last_state = None; t_stage2 = None
    try:
        while True:
            row = sample()
            if row["state"] != last_state:
                say(f"Zustand {last_state} -> {row['state']} (Limit {row['limit_w']} W, Ziel {row['target_w']} W, hochlast_s {row['hochlast_s']}, Warnungen {row['warnungen']})")
                last_state = row["state"]
                if row["state"] == "gedrosselt":
                    t_stage2 = time.time()
            if int(time.time() - t_start) % 60 < 2:
                say(f"  t+{int(time.time() - t_start)} s: {row['state']} hochlast_s {row['hochlast_s']} pin16 {row['pin16_w']} W board {row['power_w']} W {row['pin16_v']} V limit {row['limit_w']} throttle {row['throttle']} Anfragen {len(REQS)}")
            if t_stage2 and time.time() - t_stage2 >= 40:   # Stufe 2 bestaetigt (Limit steht) -> Last weg
                break
            if time.time() - t_start > MAX_LOAD_S:
                say("Notbremse: keine Stufe 2 innerhalb der Frist"); break
            time.sleep(2)
    finally:
        stop_load.set()
        say(f"Last beendet nach {int(time.time() - t_start)} s, {len(REQS)} Anfragen")
    # Erholung beobachten, bis normal (oder Zeitlimit)
    t_rec = time.time()
    while time.time() - t_rec < RECOVER_MAX_S:
        row = sample()
        if row["state"] != last_state:
            say(f"Zustand {last_state} -> {row['state']} (Limit {row['limit_w']} W, Ziel {row['target_w']} W)")
            last_state = row["state"]
            if row["state"] == "normal":
                break
        time.sleep(2)
    ok = [r for r in REQS if r.get("status") == 200]
    summary = {"model": MODEL, "workers": WORKERS, "requests": len(REQS), "ok": len(ok),
               "prompt_tps_median": sorted(r["prompt_tps"] for r in ok if r.get("prompt_tps"))[len(ok) // 2] if ok else None,
               "states": [(time.strftime("%H:%M:%S", time.localtime(r["t"])), r["state"], r["limit_w"]) for i, r in enumerate(ROWS) if i == 0 or r["state"] != ROWS[i - 1]["state"]],
               "pin16_w_median_under_load": sorted(r["pin16_w"] for r in ROWS if r.get("pin16_w") and r["t"] < t_rec)[len([r for r in ROWS if r.get("pin16_w") and r["t"] < t_rec]) // 2] if ROWS else None,
               "pin16_v_min": min((r["pin16_v"] for r in ROWS if r.get("pin16_v")), default=None),
               "mem_temp_max": max((r["mem_temp"] for r in ROWS if r.get("mem_temp")), default=None),
               "hotspot_max": max((r["hotspot"] for r in ROWS if r.get("hotspot")), default=None),
               "sw_power_cap_share": round(sum(1 for r in ROWS if r["t"] < t_rec and "sw_power_cap" in (r.get("throttle") or [])) / max(1, sum(1 for r in ROWS if r["t"] < t_rec)), 2)}
    say("Zusammenfassung:", json.dumps(summary, ensure_ascii=False))
    json.dump({"summary": summary, "rows": ROWS, "requests": REQS, "log": LOG}, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


main()
