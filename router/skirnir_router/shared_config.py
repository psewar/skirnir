"""Server-owned memory scope definitions; never accept upstream bank IDs from clients.

Parsers return the value or a Problem (outcome.py)."""
import re

from .outcome import failed, invalid


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        return invalid("invalid scope alias")
    return value


def parse_scopes(raw, classes):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 256:
        return invalid("memory_scopes must be a bounded mapping")
    result = {}
    for alias, spec in raw.items():
        if failed(checked := name(alias)):
            return checked
        if not isinstance(spec, dict) or set(spec) - {"data_class", "allow_cloud", "writers"} or spec.get("data_class") not in classes:
            return invalid("memory scope requires a data_class")
        if type(spec.get("allow_cloud", False)) is not bool:
            return invalid("scope allow_cloud must be boolean")
        # ADR-0060: the scope names who may write into it. Empty or absent means nobody; a client's write grant alone is
        # not enough (config.py refuses a grant whose client is not listed here, memory.py checks it again per request).
        writers = spec.get("writers", [])
        if (not isinstance(writers, list) or len(writers) > 64
                or any(not isinstance(w, str) or not w or len(w) > 128 or any(ord(c) < 32 for c in w) for w in writers)
                or len(set(writers)) != len(writers)):
            return invalid(f"memory_scopes.{alias}.writers must be a list of at most 64 distinct client names")
        result[alias] = {"data_class": spec["data_class"], "allow_cloud": spec.get("allow_cloud", False), "writers": tuple(writers)}
    return result
