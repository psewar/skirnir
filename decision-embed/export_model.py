#!/usr/bin/env python3
"""Stufe 2, Schritt 1: ein mehrsprachiges Satz-Embedding-Modell nach ONNX exportieren und dynamisch auf int8 quantisieren.

    ..\\decision-jevlike\\.venv\\Scripts\\python export_model.py [--model intfloat/multilingual-e5-small] [--out models/e5-small]
Ergebnis in <out>/: model.onnx (fp32), model_quantized.onnx (int8, dynamisch), tokenizer.json + Konfiguration, export.json.
Der Download passiert nur hier (Setup); Training und Dienst laufen danach offline aus diesem Ordner.
"""

import argparse
import json
import os
import time

ap = argparse.ArgumentParser()
ap.add_argument("--model", default="intfloat/multilingual-e5-small")
ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "e5-small"))
a = ap.parse_args()
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from optimum.onnxruntime import ORTModelForFeatureExtraction, ORTQuantizer  # noqa: E402
from optimum.onnxruntime.configuration import AutoQuantizationConfig  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402

t0 = time.time()
os.makedirs(a.out, exist_ok=True)
tok = AutoTokenizer.from_pretrained(a.model)
tok.save_pretrained(a.out)
model = ORTModelForFeatureExtraction.from_pretrained(a.model, export=True)
model.save_pretrained(a.out)
quantizer = ORTQuantizer.from_pretrained(a.out, file_name="model.onnx")
qconfig = AutoQuantizationConfig.avx512_vnni(is_static=False, per_channel=False)   # dynamische int8-Quantisierung, CPU
quantizer.quantize(save_dir=a.out, quantization_config=qconfig)
sizes = {f: round(os.path.getsize(os.path.join(a.out, f)) / 2**20, 1) for f in os.listdir(a.out) if f.endswith(".onnx")}
info = {"model": a.model, "exported_at": time.strftime("%Y-%m-%d %H:%M"), "files_mb": sizes, "hidden": model.config.hidden_size,
        "layers": model.config.num_hidden_layers, "prefix": "query: " if "e5" in a.model else ""}
json.dump(info, open(os.path.join(a.out, "export.json"), "w"), indent=1)
print(json.dumps(info, indent=1), f"\n{time.time() - t0:.0f} s")
