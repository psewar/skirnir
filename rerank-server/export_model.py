#!/usr/bin/env python3
"""Prepare Cross-Encoder for the Reranker service - one-time, requires network. Two steps, as the service does not need torch:

    <venv with torch+transformers>/python export_model.py onnx --model cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 --out models/mmarco-mMiniLMv2-L12
    <service venv>/python export_model.py ir --out models/mmarco-mMiniLMv2-L12

`onnx`: model.onnx, tokenizer.json and reference.json (Token-IDs and Torch-fp32-Logits for test pairs).
`ir`:   OpenVINO-IR in fp16 (model.xml/.bin). Verifies beforehand that the tokenizers library yields the same IDs as transformers
        and that the IR produces the same order as Torch; only then writes manifest.json with the SHA-256 of the runtime files.
        Without manifest.json, the service will not start.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rerank_service import SELFTEST_QUERY, SELFTEST_TEXTS, sha256_file  # noqa: E402

# Test pairs: the self-test pair and a few German/English sentences with clear ranking
CHECK_QUERY = "Wann darf ich eine Auslieferung als fertig melden?"
CHECK_TEXTS = [
    "Eine Auslieferung gilt erst als fertig, wenn die neue Version im Cluster laeuft.",
    "Bananen sind gelb und wachsen in warmen Laendern.",
    "A deployment counts as done only once the new version is running.",
    "Der Garten wird im Herbst umgegraben.",
]


def need(*modules: str) -> str:
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    return ", ".join(missing)


def step_onnx(model_id: str, out: str) -> int:
    missing = need("torch", "transformers")
    if missing:
        print(f"Schritt onnx braucht: {missing}", file=sys.stderr)
        return 2
    import inspect

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    os.makedirs(out, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(model_id)
    if not tok.is_fast:
        print("kein schneller Tokenizer (tokenizer.json) - fuer den Dienst nicht nutzbar", file=sys.stderr)
        return 2
    model = AutoModelForSequenceClassification.from_pretrained(model_id, dtype=torch.float32).eval()
    if model.config.num_labels != 1:
        print(f"kein Cross-Encoder mit einem Score (num_labels={model.config.num_labels})", file=sys.stderr)
        return 2
    tok.backend_tokenizer.save(os.path.join(out, "tokenizer.json"))
    # input_names are assigned positionally: therefore in the order of the forward signature (otherwise torch.onnx swaps them
    # for BERT attention_mask and token_type_ids - measured 2026-10-20: Spearman 0.15 on all devices)
    signature = list(inspect.signature(model.forward).parameters)
    inputs = sorted((k for k in tok.model_input_names if k in signature), key=signature.index)
    pairs = [(SELFTEST_QUERY, t) for t in SELFTEST_TEXTS] + [(CHECK_QUERY, t) for t in CHECK_TEXTS]
    enc = tok([q for q, _ in pairs], [t for _, t in pairs], padding=True, truncation=True, max_length=512, return_tensors="pt")
    with torch.inference_mode():
        logits = model(**{k: enc[k] for k in inputs}).logits.view(-1).tolist()
    sample = {k: enc[k][:2] for k in inputs}
    torch.onnx.export(model, (sample,), os.path.join(out, "model.onnx"), input_names=inputs, output_names=["logits"],
                      dynamic_axes={k: {0: "batch", 1: "seq"} for k in inputs} | {"logits": {0: "batch"}},
                      opset_version=17, dynamo=False)
    revision = getattr(model.config, "_commit_hash", None)
    json.dump({"model_id": model_id, "revision": revision, "inputs": inputs, "max_length": 512,
               "pad_id": tok.pad_token_id, "pad_token": tok.pad_token, "pairs": pairs, "logits": logits,
               "input_ids": [[int(x) for x, m in zip(ids, mask, strict=True) if m]
                             for ids, mask in zip(enc["input_ids"].tolist(), enc["attention_mask"].tolist(), strict=True)]},
              open(os.path.join(out, "reference.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"onnx: {model_id} ({revision}) -> {out}, Eingaben {inputs}")
    return 0


def step_ir(out: str) -> int:
    missing = need("openvino", "tokenizers", "numpy")
    if missing:
        print(f"Schritt ir braucht: {missing}", file=sys.stderr)
        return 2
    import numpy as np
    import openvino as ov
    from tokenizers import Tokenizer

    ref_path = os.path.join(out, "reference.json")
    if not os.path.isfile(ref_path) or not os.path.isfile(os.path.join(out, "model.onnx")):
        print("erst Schritt onnx ausfuehren (reference.json/model.onnx fehlen)", file=sys.stderr)
        return 2
    ref = json.load(open(ref_path, encoding="utf-8"))
    tok = Tokenizer.from_file(os.path.join(out, "tokenizer.json"))
    tok.enable_truncation(max_length=ref["max_length"], strategy="longest_first")
    ids = [e.ids for e in tok.encode_batch([tuple(p) for p in ref["pairs"]])]
    if ids != ref["input_ids"]:
        print("ABBRUCH: tokenizers liefert andere Token-IDs als transformers - Dienst wuerde falsch rechnen", file=sys.stderr)
        return 1
    model = ov.convert_model(os.path.join(out, "model.onnx"))
    ov.save_model(model, os.path.join(out, "model.xml"), compress_to_fp16=True)
    tok.enable_padding(pad_id=int(ref["pad_id"]), pad_token=str(ref["pad_token"]))
    enc = tok.encode_batch([tuple(p) for p in ref["pairs"]])
    field = {"input_ids": "ids", "attention_mask": "attention_mask", "token_type_ids": "type_ids"}
    feed = {k: np.array([getattr(e, field[k]) for e in enc], dtype=np.int64) for k in ref["inputs"]}
    compiled = ov.Core().compile_model(os.path.join(out, "model.xml"), "CPU")
    got = np.asarray(compiled.create_infer_request().infer(feed)[compiled.output(0)]).reshape(-1)
    want = np.asarray(ref["logits"])
    if not np.all(np.isfinite(got)) or np.max(np.abs(got - want)) > 0.05 or not got[0] > got[1]:
        print(f"ABBRUCH: IR weicht von Torch ab (max {np.max(np.abs(got - want)):.4f}) oder Selbsttest-Paar falsch", file=sys.stderr)
        return 1
    files = {n: sha256_file(os.path.join(out, n)) for n in ("model.xml", "model.bin", "tokenizer.json")}
    manifest = {k: ref[k] for k in ("model_id", "revision", "inputs", "max_length", "pad_id", "pad_token")} | {"files": files}
    json.dump(manifest, open(os.path.join(out, "manifest.json"), "w", encoding="utf-8"), indent=1)
    print(f"ir: {out} fp16, Abweichung zu Torch max {np.max(np.abs(got - want)):.4f}, manifest.json geschrieben")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("onnx", "ir"))
    ap.add_argument("--model", default="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1")
    ap.add_argument("--out", default=os.path.join(HERE, "models", "mmarco-mMiniLMv2-L12"))
    a = ap.parse_args(argv)
    return step_onnx(a.model, a.out) if a.step == "onnx" else step_ir(a.out)


if __name__ == "__main__":
    sys.exit(main())
