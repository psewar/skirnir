#!/usr/bin/env python3
"""Performance-Messstand fuer den Router (2026-09-16): Fake-Knoten mit kurzer Antwortzeit, Router ohne TLS/Auth
(abgeleitete perf-config.yaml wie dev_env), Lasttest ueber /admin/loadtest plus UI-artiges Polling (/metrics,
/admin/state, /admin/config alle 3 s). Misst CPU-Zeit des Router-Prozesses je Anfrage und nimmt mit py-spy ein
Sampling-Profil (raw, gestapelte Stacks) auf. Ergebnis: perf-result.json + perf-profile.txt im Testordner.

    python perf_run.py [--n 600] [--c 8] [--delay 0.02] [--label vorher]
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
C = "http://127.0.0.1:21435"


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


N, CONC, DELAY, LABEL, ROUNDS = arg("--n", 300), arg("--c", 8), arg("--delay", 0.02), arg("--label", "lauf"), arg("--rounds", 12)
SPY_S = 30   # py-spy schreibt sein Profil nur bei regulaerem Ende -> feste Dauer, Last laeuft mindestens so lange


def http(path, body=None, timeout=300):
    req = urllib.request.Request(C + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def cpu_seconds(pid):
    out = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {pid}).TotalProcessorTime.TotalSeconds"],
                         capture_output=True, text=True).stdout.strip()
    return float(out.replace(",", "."))


def main():
    cfg = yaml.safe_load(open(os.path.join(HERE, "test-config.yaml"), encoding="utf-8"))
    cfg["router"].pop("control_tls", None)
    cfg["router"].pop("control_auth", None)
    cfg["router"]["api_tls"] = False
    cfg["router"]["public_url"] = C
    cfg["router"]["client_auth"]["mode"] = "observe"
    cfg.setdefault("modes", {}).setdefault("admission", {})["max_inflight_default"] = 16
    open(os.path.join(HERE, "perf-config.yaml"), "w", encoding="utf-8").write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    open(os.path.join(HERE, "secrets.env"), "w", encoding="utf-8").write("CLOUD_TEST_KEY=cloud-test-key\nCLAUDE_TEST_KEY=claude-test-key\n")
    for f in ("roles.yaml", "nodes.json", "usage.json", "perf.json"):
        try:
            os.remove(os.path.join(HERE, f))
        except FileNotFoundError:
            pass
    procs, logs = [], {}

    def start(name, args):
        logs[name] = open(os.path.join(HERE, f"perf-{name}.log"), "w")
        p = subprocess.Popen(args, cwd=HERE, stdout=logs[name], stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    try:
        start("big", [PY, "fake_ollama.py", "21001", "big", "qwen3.6:35b-a3b,qwen3-coder:30b,granite4.2:8b,local-assist:latest,gpt-oss:20b",
                      "--delay", str(DELAY), "--tls", "test-cert.pem", "test-key.pem", "--require-token", "testtoken"])
        start("small", [PY, "fake_ollama.py", "21002", "small", "granite4.2:8b", "--delay", str(DELAY)])
        start("cloud", [PY, "fake_cloud.py", "21010", "cloud-test-key", "claude-test-key"])
        router = start("router", [PY, os.path.join(HERE, "..", "router", "router.py"), "perf-config.yaml"])
        time.sleep(2.5)
        start("agent", [PY, "fake_agent.py", "ws://127.0.0.1:21435", "big", "https://127.0.0.1:21001", "fake-agent-big.key", "testtoken"])
        for _ in range(40):   # Agent freigeben, sobald er im Register steht
            time.sleep(0.5)
            try:
                nodes = json.loads(http("/admin/nodes")[1])["nodes"]
            except Exception:  # noqa: BLE001
                continue
            big = next((n for n in nodes if n["name"] == "big"), None)
            if big and big.get("state") == "pending":
                http(f"/admin/nodes/{big['fp']}/approve", {"wol": False, "weight": 3, "mqtt": False, "foreign_vram_baseline_gib": 0.5})
            if big and big.get("state") == "approved":
                http(f"/admin/nodes/{big['fp']}/policy", {"max_inflight": 16})
                break
        for _ in range(40):   # warten bis der Knoten online/free ist
            st = json.loads(http("/admin/state")[1])
            if st["nodes"].get("big", {}).get("state") == "free":
                break
            time.sleep(0.5)
        http("/admin/loadtest", {"model": "standard:latest", "n": 20, "concurrency": 4, "prompt": "warm"})   # Aufwaermen (Caches, JIT-freie Pfade)
        cpu0, t0 = cpu_seconds(router.pid), time.time()
        prof = os.path.join(HERE, "perf-profile.txt")
        spy = subprocess.Popen(["py-spy", "record", "--pid", str(router.pid), "--duration", str(SPY_S), "--rate", "250", "--format", "raw",
                                "--nonblocking", "-o", prof], stdout=subprocess.DEVNULL, stderr=open(os.path.join(HERE, "perf-pyspy.log"), "w"))
        time.sleep(1.0)

        stop = {"v": False}

        def poller():   # UI-Verhalten: alle 3 s /metrics, /admin/state, jede 5. Runde /admin/config
            k = 0
            while not stop["v"]:
                for path in ("/metrics", "/admin/state") + (("/admin/config",) if k % 5 == 0 else ()):
                    try:
                        http(path, timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
                k += 1
                time.sleep(3)

        import threading
        th = threading.Thread(target=poller, daemon=True)
        th.start()
        rounds, done = [], 0
        while done < ROUNDS or time.time() - t0 < SPY_S + 2:   # so viele Runden wie verlangt, mindestens aber die Profil-Dauer
            st, raw = http("/admin/loadtest", {"model": "standard:latest", "n": N, "concurrency": CONC, "probe_interactive": True, "prompt": "Antworte nur mit OK."})
            rounds.append(json.loads(raw)); done += 1
        stop["v"] = True
        cpu1, t1 = cpu_seconds(router.pid), time.time()
        try:
            spy.wait(60)
        except subprocess.TimeoutExpired:
            spy.kill()
        total = sum(r["n"] for r in rounds); secs = sum(r["seconds"] for r in rounds); okc = sum(r["normal"]["ok"] for r in rounds)
        avg = lambda k: round(sum((r["normal"][k] or 0) * r["n"] for r in rounds) / total, 4)  # noqa: E731
        res = {"label": LABEL, "requests": total, "rounds": len(rounds), "concurrency": CONC, "fake_delay_s": DELAY, "load_seconds": round(secs, 2),
               "throughput_rps": round(total / secs, 1), "router_cpu_s": round(cpu1 - cpu0, 3), "router_cpu_ms_per_request": round((cpu1 - cpu0) * 1000 / total, 3),
               "cpu_util_pct": round(100 * (cpu1 - cpu0) / (t1 - t0), 1), "wall_p50": avg("wall_p50"), "wall_p95": avg("wall_p95"),
               "queue_mean": avg("queue_mean"), "ok": okc, "errors": [e for r in rounds for e in r["normal"]["errors"]][:5],
               "interactive_p50": round(sum(((r.get("interactive") or {}).get("wall_p50") or 0) for r in rounds) / len(rounds), 4)}
        print(json.dumps(res, indent=1))
        out = os.path.join(HERE, "perf-result.json")
        hist = json.load(open(out)) if os.path.exists(out) else []
        hist.append(res)
        json.dump(hist, open(out, "w"), indent=1)
    finally:
        for p in procs:
            p.terminate()
        for f in logs.values():
            f.close()


if __name__ == "__main__":
    main()
