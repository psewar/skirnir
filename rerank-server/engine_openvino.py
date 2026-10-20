"""Cross-Encoder auf OpenVINO (CPU, iGPU, NPU) mit dem Tokenizer aus tokenizer.json. Zur Laufzeit nur openvino, tokenizers, numpy.

Laenge dynamisch, auf ein Vielfaches von `pad_multiple` aufgerundet (weniger verschiedene Formen = weniger Kernel-Kompilate
auf der GPU), Batches zu `batch` Paaren.
"""

from __future__ import annotations

import importlib.util
import os

import numpy as np

from rerank_service import verify_manifest


class OpenVinoEngine:
    def __init__(self, compiled, tokenizer, inputs: list[str], manifest: dict, device: str, batch: int):
        self._compiled, self._tok, self._inputs = compiled, tokenizer, inputs
        self._manifest, self._device, self._batch = manifest, device, batch
        self._output = compiled.output(0)

    def describe(self) -> dict:
        return {"model": self._manifest.get("model_id"), "revision": self._manifest.get("revision"),
                "device": self._device, "inputs": self._inputs}

    def score(self, query: str, texts: list[str]) -> list[float]:
        out: list[float] = []
        request = self._compiled.create_infer_request()
        for start in range(0, len(texts), self._batch):
            encodings = self._tok.encode_batch([(query, t) for t in texts[start:start + self._batch]])
            feed = {}
            for name in self._inputs:
                field = {"input_ids": "ids", "attention_mask": "attention_mask", "token_type_ids": "type_ids"}[name]
                feed[name] = np.array([getattr(e, field) for e in encodings], dtype=np.int64)
            logits = np.asarray(request.infer(feed)[self._output]).reshape(-1)
            out.extend(float(x) for x in logits)
        return out


def build(model_dir: str, device: str, max_length: int = 512, pad_multiple: int = 32, batch: int = 64):
    """(engine, "") oder (None, grund). Prueft vorher Bibliotheken, Manifest/Pruefsummen und Geraet."""
    missing = [m for m in ("openvino", "tokenizers") if importlib.util.find_spec(m) is None]
    if missing:
        return None, "missing_module:" + ",".join(missing)
    manifest, reason = verify_manifest(model_dir)
    if manifest is None:
        return None, reason
    import openvino as ov
    from tokenizers import Tokenizer

    core = ov.Core()
    if device not in core.available_devices:
        return None, f"device_unavailable:{device}"
    inputs = manifest.get("inputs") or []
    if not inputs or any(i not in ("input_ids", "attention_mask", "token_type_ids") for i in inputs):
        return None, "manifest_inputs_invalid"
    tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
    tok.enable_truncation(max_length=min(max_length, int(manifest.get("max_length", max_length))), strategy="longest_first")
    tok.enable_padding(pad_id=int(manifest.get("pad_id", 0)), pad_token=str(manifest.get("pad_token", "<pad>")),
                       pad_to_multiple_of=pad_multiple)
    compiled = core.compile_model(os.path.join(model_dir, "model.xml"), device)
    return OpenVinoEngine(compiled, tok, inputs, manifest, device, batch), ""
