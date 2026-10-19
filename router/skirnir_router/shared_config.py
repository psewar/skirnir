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
        if not isinstance(spec, dict) or set(spec) - {"data_class", "allow_cloud"} or spec.get("data_class") not in classes:
            return invalid("memory scope requires a data_class")
        if type(spec.get("allow_cloud", False)) is not bool:
            return invalid("scope allow_cloud must be boolean")
        result[alias] = {"data_class": spec["data_class"], "allow_cloud": spec.get("allow_cloud", False)}
    return result
