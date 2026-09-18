#!/usr/bin/env python3
"""Embedding-Dienst (Stufe 2) auf router-host (LXC) ausrollen: Modellordner + Code nach /opt/skirnir-embed, Container bauen
und starten, Gesundheit und Latenz messen. Nutzt die Helfer aus deploy/deploy.py (Ops-Ordner, Secret-Store, Container-Push).

    python deploy_ct.py [--no-build] [--measure-only]
Der Dienst bindet nur 127.0.0.1:8082 im CT; der Router erreicht ihn ueber decision_engine.embed.endpoint.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "deploy"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from deploy import connect, ct_exec, ct_push, run  # noqa: E402

MODEL_DIR = os.path.join(HERE, "models", "e5-small")
MODEL_FILES = ["model_quantized.onnx", "tokenizer.json", "config.json", "export.json", "head.json", "tokenizer_config.json", "special_tokens_map.json"]
CODE_FILES = ["server.py", "embedder.py", "Dockerfile", "compose.yaml"]
REMOTE = "/opt/skirnir-embed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--measure-only", action="store_true")
    a = ap.parse_args()
    c = connect()
    try:
        if not a.measure_only:
            run(c, "mkdir -p /tmp/skirnir-embed/models")
            sftp = c.open_sftp()
            for f in MODEL_FILES:
                sftp.put(os.path.join(MODEL_DIR, f), f"/tmp/skirnir-embed/models/{f}")
                print("hochgeladen", f, f"{os.path.getsize(os.path.join(MODEL_DIR, f)) / 2**20:.1f} MB", flush=True)
            for f in CODE_FILES:
                sftp.put(os.path.join(HERE, f), f"/tmp/skirnir-embed/{f}")
            sftp.close()
            run(c, ct_exec(f"mkdir -p {REMOTE}/models/e5-small"))
            for f in MODEL_FILES:
                rc, out, err = run(c, ct_push(f"/tmp/skirnir-embed/models/{f}", f"{REMOTE}/models/e5-small/{f}", "0644"), timeout=600)
                if rc:
                    raise SystemExit(f"push {f}: {err}")
            for f in CODE_FILES:
                run(c, ct_push(f"/tmp/skirnir-embed/{f}", f"{REMOTE}/{f}", "0644"))
            print("Dateien im CT", flush=True)
            if not a.no_build:
                rc, out, err = run(c, ct_exec(f"sh -c 'cd {REMOTE} && docker compose up -d --build 2>&1 | tail -5'"), timeout=1500)
                print(out[-1500:], err[-500:])
        for _ in range(60):
            rc, out, err = run(c, ct_exec("curl -s -m 5 http://127.0.0.1:8082/health"))
            if out.strip().startswith("{"):
                print("health:", out.strip()[:300])
                break
            time.sleep(3)
        else:
            rc, out, err = run(c, ct_exec("docker logs --tail 20 skirnir-embed"))
            raise SystemExit("Dienst antwortet nicht: " + out[-800:] + err[-300:])
        # Latenz im CT: 40 Einzelanfragen, gemessen von curl (inkl. HTTP), Median/p95
        body = json.dumps({"context": "Schalte das Licht im Wohnzimmer an und dimm es auf 40 Prozent", "options": ["standard", "gross", "assist", "code"]})
        script = ("for i in $(seq 1 40); do curl -s -o /dev/null -w '%{time_total}\\n' -m 10 -X POST -H 'Content-Type: application/json' "
                  f"-d '{body}' http://127.0.0.1:8082/decide; done | sort -n | awk '{{a[NR]=$1}} END {{printf \"p50 %.1f ms  p95 %.1f ms  n %d\\n\", a[int(NR/2)]*1000, a[int(NR*0.95)]*1000, NR}}'")
        rc, out, err = run(c, ct_exec(f"sh -c \"{script}\""), timeout=300)
        print("Latenz im CT (curl, inkl. HTTP):", out.strip(), err.strip()[:200])
        rc, out, err = run(c, ct_exec("docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}' skirnir-embed"))
        print("Container:", out.strip())
    finally:
        c.close()


if __name__ == "__main__":
    main()
