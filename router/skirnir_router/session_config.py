"""Strict opt-in configuration; no database driver is imported here."""

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionConfig:
    enabled: bool = False
    dsn_env: str = "SKIRNIR_SESSION_DSN"
    min_pool_size: int = 1
    max_pool_size: int = 5
    timeout_s: float = 5.0


def parse_session_config(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("router.session_store must be a mapping")
    defaults = SessionConfig()
    if set(raw) - set(defaults.__dataclass_fields__):
        raise ValueError("unknown router.session_store key")
    values = {k: raw.get(k, getattr(defaults, k)) for k in defaults.__dataclass_fields__}
    if type(values["enabled"]) is not bool:
        raise ValueError("session_store.enabled must be boolean")
    if not isinstance(values["dsn_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", values["dsn_env"]):
        raise ValueError("session_store.dsn_env must name an environment variable")
    for k in ("min_pool_size", "max_pool_size"):
        if type(values[k]) is not int or not 1 <= values[k] <= 50:
            raise ValueError(f"session_store.{k} must be an integer between 1 and 50")
    if values["min_pool_size"] > values["max_pool_size"]:
        raise ValueError("session_store.min_pool_size must not exceed max_pool_size")
    timeout = values["timeout_s"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 60:
        raise ValueError("session_store.timeout_s must be between 0.1 and 60")
    return SessionConfig(**values)
