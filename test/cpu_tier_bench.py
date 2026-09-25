#!/usr/bin/env python3
"""Messung: Modell rein auf der CPU (num_gpu 0) waehrend ein Spiel die GPU haelt.
Fragen: Ladezeit, gen tok/s, prompt tok/s (kurz und ~1.5k Token), CPU-Last dabei, bleibt das VRAM unangetastet?
Direkt gegen das lokale Ollama, nicht ueber den Router. Aufruf:
  python cpu_tier_bench.py <modell> [num_thread ...]     z. B. cpu_tier_bench.py gpt-oss:20b 0 8 16
"""
import json
import subprocess
import sys
import threading
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = sys.argv[1]
THREADS = [int(x) for x in sys.argv[2:]] or [0, 8]
CTX = 8192
LONG_PROMPT = ("Fasse den folgenden Text in drei Saetzen zusammen.\n\n" +
               ("Ein Smart Home besteht aus einer Hausautomation, mehreren Sprachsatelliten, einer lokalen Spracherkennung "
                "und einem Skirnir-Router, der Anfragen an einen GPU-Rechner verteilt. Wird die GPU anderweitig gebraucht, "
                "entlaedt der Router das grosse Modell und ein kleines Modell uebernimmt. ") * 60)


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def gpu():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip().split(",")
    return int(out[0]), int(out[1])


class CPUSampler(threading.Thread):
    """Mittelt die Gesamt-CPU-Last (typeperf) waehrend eines Abschnitts."""
    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self.stop = [], threading.Event()

    def run(self):
        while not self.stop.is_set():
            out = subprocess.run(["typeperf", r"\Processor(_Total)\% Processor Time", "-sc", "1"],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                if line.startswith('"') and "," in line and "Processor" not in line:
                    try:
                        self.samples.append(float(line.split(",")[1].strip('"')))
                    except ValueError:
                        pass
            time.sleep(0.5)

    def avg(self):
        return sum(self.samples) / len(self.samples) if self.samples else float("nan")


def run_case(threads):
    opts = {"num_gpu": 0, "num_ctx": CTX}
    if threads:
        opts["num_thread"] = threads
    label = f"num_thread={threads or 'default'}"
    v0, u0 = gpu()
    t0 = time.time()
    r = post("/api/generate", {"model": MODEL, "prompt": "Antworte nur mit OK.", "stream": False, "keep_alive": "10m", "options": opts})
    load_s = r.get("load_duration", 0) / 1e9
    ps = post("/api/ps", {}) if False else json.loads(urllib.request.urlopen(OLLAMA + "/api/ps", timeout=10).read())
    inst = next((m for m in ps.get("models", []) if m["name"] == MODEL or m["model"] == MODEL), {})
    v1, u1 = gpu()
    print(f"[{label}] geladen in {load_s:.1f} s (Wall {time.time()-t0:.1f} s); /api/ps size_vram={inst.get('size_vram',0)/2**30:.2f} GiB, "
          f"size={inst.get('size',0)/2**30:.1f} GiB; VRAM vorher {v0} MiB -> nachher {v1} MiB")
    # kurze Frage
    smp = CPUSampler(); smp.start()
    r = post("/api/generate", {"model": MODEL, "prompt": "Wozu dient ein Skirnir-Router? Antworte in zwei Saetzen.", "stream": False,
                               "keep_alive": "10m", "options": {**opts, "temperature": 0}, "think": False})
    smp.stop.set(); smp.join(timeout=3)
    gen = r["eval_count"] / (r["eval_duration"] / 1e9)
    pr = r["prompt_eval_count"] / max(r.get("prompt_eval_duration", 1) / 1e9, 1e-9)
    print(f"[{label}] kurz: {r['eval_count']} Token gen {gen:.1f} tok/s, prompt {r['prompt_eval_count']} Token {pr:.0f} tok/s, "
          f"total {r['total_duration']/1e9:.1f} s, CPU {smp.avg():.0f} %, GPU util {gpu()[1]} %")
    # langer Prompt
    smp = CPUSampler(); smp.start()
    r = post("/api/generate", {"model": MODEL, "prompt": LONG_PROMPT, "stream": False, "keep_alive": "10m",
                               "options": {**opts, "temperature": 0, "num_predict": 60}, "think": False})
    smp.stop.set(); smp.join(timeout=3)
    pr = r["prompt_eval_count"] / max(r.get("prompt_eval_duration", 1) / 1e9, 1e-9)
    gen = r["eval_count"] / max(r["eval_duration"] / 1e9, 1e-9)
    print(f"[{label}] lang: prompt {r['prompt_eval_count']} Token {pr:.0f} tok/s ({r.get('prompt_eval_duration',0)/1e9:.1f} s), "
          f"gen {gen:.1f} tok/s, total {r['total_duration']/1e9:.1f} s, CPU {smp.avg():.0f} %")


if __name__ == "__main__":
    print(f"Modell {MODEL}, num_ctx {CTX}; GPU vorher: {gpu()}")
    try:
        for th in THREADS:
            run_case(th)
    finally:
        post("/api/generate", {"model": MODEL, "keep_alive": 0}, timeout=60)
        time.sleep(2)
        print(f"entladen; GPU nachher: {gpu()}")
