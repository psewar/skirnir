"""Gemeinsamer Embedding-Kern fuer Training (train_head.py) und Dienst (server.py): ONNX Runtime auf CPU, HF-Tokenizer,
Mean-Pooling ueber die Aufmerksamkeitsmaske, L2-Norm. Kein torch, kein transformers zur Laufzeit."""

from __future__ import annotations

import json
import os

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


class Embedder:
    def __init__(self, model_dir: str, quantized: bool = True, threads: int | None = None, max_tokens: int = 256):
        info = json.load(open(os.path.join(model_dir, "export.json"), encoding="utf-8")) if os.path.exists(os.path.join(model_dir, "export.json")) else {}
        self.prefix = info.get("prefix", "")
        self.name = info.get("model", os.path.basename(model_dir))
        fn = "model_quantized.onnx" if quantized and os.path.exists(os.path.join(model_dir, "model_quantized.onnx")) else "model.onnx"
        self.file = fn
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(os.path.join(model_dir, fn), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tokenizer.enable_truncation(max_tokens)
        self.tokenizer.enable_padding(pad_id=self._pad_id(model_dir), pad_token="<pad>")
        self.dim = None

    def _pad_id(self, model_dir):
        try:
            cfg = json.load(open(os.path.join(model_dir, "config.json"), encoding="utf-8"))
            return int(cfg.get("pad_token_id", 0) or 0)
        except (OSError, ValueError):
            return 0

    def encode(self, texts: list[str]) -> np.ndarray:
        enc = self.tokenizer.encode_batch([self.prefix + t for t in texts])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feed)[0]                       # (batch, seq, dim)
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)   # Mean-Pooling
        pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
        self.dim = pooled.shape[1]
        return pooled.astype(np.float32)
