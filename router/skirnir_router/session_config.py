"""Strict opt-in configuration; no database driver is imported here."""

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionConfig:
    enabled: bool = False
    handoff_enabled: bool = False
    handoff_key_env: str = "SKIRNIR_HANDOFF_KEY"
    dsn_env: str = "SKIRNIR_SESSION_DSN"
    min_pool_size: int = 1
    max_pool_size: int = 5
    timeout_s: float = 5.0
    retention_days: int = 0
    archived_retention_days: int = 0
    maintenance_interval_s: int = 300


def parse_session_config(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("router.session_store must be a mapping")
    defaults = SessionConfig()
    if set(raw) - set(defaults.__dataclass_fields__):
        raise ValueError("unknown router.session_store key")
    values = {k: raw.get(k, getattr(defaults, k)) for k in defaults.__dataclass_fields__}
    if type(values["handoff_enabled"]) is not bool or (values["handoff_enabled"] and not values["enabled"]):
        raise ValueError("handoff_enabled requires session_store.enabled")
    if not isinstance(values["handoff_key_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", values["handoff_key_env"]):
        raise ValueError("handoff_key_env must name an environment variable")
    if type(values["enabled"]) is not bool:
        raise ValueError("session_store.enabled must be boolean")
    if not isinstance(values["dsn_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", values["dsn_env"]):
        raise ValueError("session_store.dsn_env must name an environment variable")
    for k in ("min_pool_size", "max_pool_size"):
        if type(values[k]) is not int or not 1 <= values[k] <= 50:
            raise ValueError(f"session_store.{k} must be an integer between 1 and 50")
    if values["min_pool_size"] > values["max_pool_size"]:
        raise ValueError("session_store.min_pool_size must not exceed max_pool_size")
    for key, low, high in (("retention_days", 0, 36500), ("archived_retention_days", 0, 36500), ("maintenance_interval_s", 10, 86400)):
        if type(values[key]) is not int or not low <= values[key] <= high:
            raise ValueError(f"session_store.{key} out of range")
    timeout = values["timeout_s"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 60:
        raise ValueError("session_store.timeout_s must be between 0.1 and 60")
    return SessionConfig(**values)
