"""Strict opt-in context management limits; no model/service dependencies."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextConfig:
    enabled: bool = False
    reserve_tokens: int = 1024
    safety_tokens: int = 256
    tail_messages: int = 8
    summary_bytes: int = 2048
    max_history_bytes: int = 2097152
    max_history_messages: int = 4096
    max_response_bytes: int = 1048576
    lease_s: int = 90


def parse_context_config(raw):
    if raw is None:
        raw = {}
    defaults = ContextConfig()
    if not isinstance(raw, dict) or set(raw) - set(defaults.__dataclass_fields__):
        raise ValueError("unknown router.context_manager configuration")
    values = {k: raw.get(k, getattr(defaults, k)) for k in defaults.__dataclass_fields__}
    if type(values["enabled"]) is not bool:
        raise ValueError("context_manager.enabled must be boolean")
    ranges = {"reserve_tokens": (1, 131072), "safety_tokens": (128, 131072), "tail_messages": (2, 256),
              "summary_bytes": (256, 65536), "max_history_bytes": (4096, 16777216),
              "max_history_messages": (16, 65536), "max_response_bytes": (1024, 16777216), "lease_s": (15, 3600)}
    for key, (low, high) in ranges.items():
        if type(values[key]) is not int or not low <= values[key] <= high:
            raise ValueError(f"context_manager.{key} must be an integer between {low} and {high}")
    return ContextConfig(**values)
