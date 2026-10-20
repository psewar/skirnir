"""Strict opt-in configuration of the rerank endpoint (rerank.py). Returns the value or a Problem (outcome.py)."""
import math
from dataclasses import dataclass

from .outcome import invalid


@dataclass(frozen=True)
class RerankConfig:
    enabled: bool = False
    nodes: tuple = ()             # node names in order of preference; the first usable one serves
    timeout_s: float = 2.0        # whole exchange with the node, tunnel included
    busy_ok: bool = True          # a busy node (game, foreign VRAM) may still rerank, e.g. on an integrated GPU
    max_texts: int = 512
    max_body_bytes: int = 4194304


def parse_rerank_config(raw):
    if raw is None:
        raw = {}
    defaults = RerankConfig()
    if not isinstance(raw, dict) or set(raw) - set(defaults.__dataclass_fields__):
        return invalid("unknown router.rerank configuration")
    values = {k: raw.get(k, getattr(defaults, k)) for k in defaults.__dataclass_fields__}
    for k in ("enabled", "busy_ok"):
        if type(values[k]) is not bool:
            return invalid(f"rerank.{k} must be boolean")
    names = values["nodes"]
    if not isinstance(names, (list, tuple)) or any(not isinstance(n, str) or not n.strip() for n in names):
        return invalid("rerank.nodes must be a list of node names")
    if len(set(names)) != len(names):
        return invalid("rerank.nodes must not repeat a node")
    if values["enabled"] and not names:
        return invalid("rerank.enabled requires at least one node in rerank.nodes")
    values["nodes"] = tuple(names)
    timeout = values["timeout_s"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 60:
        return invalid("rerank.timeout_s must be between 0.1 and 60")
    values["timeout_s"] = float(timeout)
    for key, low, high in (("max_texts", 1, 4096), ("max_body_bytes", 1024, 67108864)):
        if type(values[key]) is not int or not low <= values[key] <= high:
            return invalid(f"rerank.{key} must be an integer between {low} and {high}")
    return RerankConfig(**values)
