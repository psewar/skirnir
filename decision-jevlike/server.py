#!/usr/bin/env python3
"""Jevlike als lokaler Dienst fuer Skirnir: ein Checkpoint, ein HTTP-Endpunkt, kein Netz nach aussen.

    python server.py --checkpoint models/skirnir-router-tiny.pt --model-name skirnir-router-tiny --host 127.0.0.1 --port 8081

Protokoll (decision/jevlike.py im Router spricht genau das):
    POST /decide  {"context": "...", "options": ["standard", "gross", ...]}
      -> {"probabilities": {"standard": 0.1, ...}, "model": "<name>", "encoder": "tiny|hf", "latency_ms": 1.9}
    GET  /health  -> {"ok": true, "model": ..., "encoder": ..., "device": ..., "offline": true, "decisions": <n>}

Offline (Abschnitt 4): HF_HUB_OFFLINE=1 und TRANSFORMERS_OFFLINE=1 werden VOR dem Import von torch/transformers gesetzt, ausser
--allow-network. Ein HF-Encoder muss also vorher im Cache liegen (setup: `python prepare_encoder.py Qwen/Qwen2.5-0.5B`).
Nur Standardbibliothek + torch + jevlike; kein aiohttp im Container noetig. Inferenz laeuft unter einem Lock (eine GPU/CPU-
Warteschlange), der Server nimmt Verbindungen in Threads an.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--model-name", default=None, help="Name im Ergebnis (Standard: Dateiname ohne .pt)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda", "mps"))
    p.add_argument("--allow-network", action="store_true", help="HF-Downloads erlauben (nur fuer Setup, nie im Betrieb)")
    p.add_argument("--threads", type=int, default=None, help="torch.set_num_threads (CPU)")
    return p.parse_args()


ARGS = parse_args()
_cache = os.path.join(os.path.dirname(os.path.abspath(ARGS.checkpoint)), "hf-cache")
if os.path.isdir(_cache):   # HF-Encoder aus dem Cache neben dem Checkpoint (prepare_encoder.py), ohne dass HF_HOME gesetzt sein muss
    os.environ.setdefault("HF_HOME", _cache)
if not ARGS.allow_network:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import torch  # noqa: E402  (nach den Offline-Variablen)
from jevlike.data import ChoiceExample  # noqa: E402
from jevlike.model import load_checkpoint, select_device  # noqa: E402
from jevlike.train import move  # noqa: E402

if ARGS.threads:
    torch.set_num_threads(ARGS.threads)
DEVICE = select_device(ARGS.device)
MODEL, COLLATOR, CONFIG = load_checkpoint(ARGS.checkpoint, DEVICE)
MODEL.eval()
MODEL_NAME = ARGS.model_name or os.path.splitext(os.path.basename(ARGS.checkpoint))[0]
LOCK = threading.Lock()
STATS = {"decisions": 0, "errors": 0, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}


def decide(context: str, options: list[str]) -> dict:
    """Eine Auswahl: Wahrscheinlichkeit je Option (Softmax ueber die Optionen, wie jevlike-predict)."""
    example = ChoiceExample(context, tuple(options), 0)
    with LOCK, torch.no_grad():
        batch = move(COLLATOR([example]), DEVICE)
        probs = MODEL(batch).softmax(-1)[0, : len(options)].cpu().tolist()
    return dict(zip(options, probs, strict=True))


class Handler(BaseHTTPRequestHandler):
    server_version = "skirnir-jevlike/0.1"

    def log_message(self, fmt, *args):   # kein Zugriffslog auf stderr (Kontexte sind Benutzertexte)
        return

    def _send(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True, "model": MODEL_NAME, "encoder": CONFIG.get("encoder"), "hf_model": CONFIG.get("hf_model"),
                                    "context_tokens": CONFIG.get("context_tokens"), "device": str(DEVICE),
                                    "offline": not ARGS.allow_network, **STATS})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/decide":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            context, options = str(body.get("context") or ""), [str(o) for o in (body.get("options") or [])]
            if not context or len(options) < 2:
                return self._send(400, {"error": "context und mindestens zwei options erwartet"})
            t0 = time.perf_counter()
            probs = decide(context, options)
            STATS["decisions"] += 1
            self._send(200, {"probabilities": probs, "model": MODEL_NAME, "encoder": CONFIG.get("encoder"),
                             "latency_ms": round((time.perf_counter() - t0) * 1000, 2)})
        except Exception as e:  # noqa: BLE001 - der Dienst darf an einer Anfrage nicht sterben
            STATS["errors"] += 1
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    print(f"skirnir-jevlike: {MODEL_NAME} ({CONFIG.get('encoder')}, {DEVICE}) auf http://{ARGS.host}:{ARGS.port} "
          f"offline={not ARGS.allow_network}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
