#!/usr/bin/env python3
"""Setup step (one-time, requires network): load a Hugging Face encoder into the local cache so that training and the service
can run offline afterwards (HF_HUB_OFFLINE=1). Stores nothing except the cache; prints the cache path.

    python prepare_encoder.py Qwen/Qwen2.5-0.5B [--cache-dir models/hf-cache]
"""
import argparse
import os

p = argparse.ArgumentParser()
p.add_argument("model")
p.add_argument("--cache-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "hf-cache"))
a = p.parse_args()
os.environ["HF_HOME"] = a.cache_dir
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
from transformers import AutoModel, AutoTokenizer  # noqa: E402

AutoTokenizer.from_pretrained(a.model)
m = AutoModel.from_pretrained(a.model)
print(f"{a.model}: {sum(x.numel() for x in m.parameters()) / 1e6:.0f} M Parameter, hidden {m.config.hidden_size}, Cache {a.cache_dir}")
print("Danach: HF_HOME auf diesen Pfad setzen und HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 - kein Download mehr noetig.")
