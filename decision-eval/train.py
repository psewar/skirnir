#!/usr/bin/env python3
"""Jevlike-Checkpoints fuer Skirnir trainieren (beide Varianten des Vorschlags) und mit jevlike-eval pruefen.

    ..\\decision-jevlike\\.venv\\Scripts\\python train.py tiny        # Variante A: Byte-Encoder, von null
    ..\\decision-jevlike\\.venv\\Scripts\\python train.py hf          # Variante B: eingefrorener Qwen/Qwen2.5-0.5B + Scoring-Head
    ... train.py hf --hf-model Qwen/Qwen2.5-0.5B --rank 256 --epochs 6

Schreibt ../decision-jevlike/models/skirnir-router-<variante>.pt. Variante B braucht den Encoder im lokalen Cache
(decision-jevlike/prepare_encoder.py, einmal mit Netz); danach laeuft alles mit HF_HUB_OFFLINE=1.
Kontextfenster 512 Bytes/Tokens statt Jevlikes 192: deutsche Saetze mit Umlaut-Ersatz sind laenger, und die Hinweise
([tools=..]) stehen am Ende.
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MODELS = os.path.join(HERE, "..", "decision-jevlike", "models")
HF_CACHE = os.path.join(MODELS, "hf-cache")

ap = argparse.ArgumentParser()
ap.add_argument("variant", choices=("tiny", "hf"))
ap.add_argument("--hf-model", default="Qwen/Qwen2.5-0.5B")
ap.add_argument("--rank", type=int, default=None)
ap.add_argument("--width", type=int, default=128)
ap.add_argument("--epochs", type=int, default=None)
ap.add_argument("--batch-size", type=int, default=None)
ap.add_argument("--learning-rate", type=float, default=None)
ap.add_argument("--context-tokens", type=int, default=512)
ap.add_argument("--option-tokens", type=int, default=16)
ap.add_argument("--device", default="auto")
ap.add_argument("--name", default=None)
a = ap.parse_args()

os.makedirs(MODELS, exist_ok=True)
name = a.name or f"skirnir-router-{a.variant}"
out = os.path.join(MODELS, name + ".pt")
env = {**os.environ, "HF_HOME": HF_CACHE, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
cmd = [sys.executable, "-m", "jevlike.train", os.path.join(DATA, "train.jsonl"), "--validation", os.path.join(DATA, "validation.jsonl"),
       "--output", out, "--context-tokens", str(a.context_tokens), "--option-tokens", str(a.option_tokens), "--device", a.device]
if a.variant == "tiny":
    cmd += ["--encoder", "tiny", "--width", str(a.width), "--rank", str(a.rank or 128), "--epochs", str(a.epochs or 40),
            "--batch-size", str(a.batch_size or 32), "--learning-rate", str(a.learning_rate or 2e-3)]
else:
    cmd += ["--encoder", "hf", "--hf-model", a.hf_model, "--rank", str(a.rank or 256), "--epochs", str(a.epochs or 8),
            "--batch-size", str(a.batch_size or 8), "--learning-rate", str(a.learning_rate or 1e-3)]
print(" ".join(cmd), flush=True)
t0 = time.time()
rc = subprocess.call(cmd, env=env)
print(f"Training rc={rc} in {time.time() - t0:.0f} s -> {out}", flush=True)
if rc == 0:
    subprocess.call([sys.executable, "-m", "jevlike.eval", out, os.path.join(DATA, "test.jsonl"), "--device", a.device], env=env)
sys.exit(rc)
