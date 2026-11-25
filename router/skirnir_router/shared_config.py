"""Server-owned memory scope definitions; never accept upstream bank IDs from clients.

Parsers return the value or a Problem (outcome.py)."""
import re

from .outcome import failed, invalid

SCOPE_FIELDS = ("data_class", "allow_cloud", "writers", "review", "reviewers", "review_expire_days", "review_max_held")
REVIEW_EXPIRE_DAYS = (14, 1, 365)     # default, minimum, maximum (ADR-0145)
REVIEW_MAX_HELD = (100, 1, 10000)


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        return invalid("invalid scope alias")
    return value


def client_names(value):
    """A list of at most 64 distinct client names, as a tuple, or None."""
    if (not isinstance(value, list) or len(value) > 64
            or any(not isinstance(w, str) or not w or len(w) > 128 or any(ord(c) < 32 for c in w) for w in value)
            or len(set(value)) != len(value)):
        return None
    return tuple(value)


def bounded(spec, key, limits):
    default, low, high = limits
    value = spec.get(key, default)
    return value if type(value) is int and low <= value <= high else None


def parse_scopes(raw, classes):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 256:
        return invalid("memory_scopes must be a bounded mapping")
    result = {}
    for alias, spec in raw.items():
        if failed(checked := name(alias)):
            return checked
        if not isinstance(spec, dict) or set(spec) - set(SCOPE_FIELDS) or spec.get("data_class") not in classes:
            return invalid("memory scope requires a data_class")
        if type(spec.get("allow_cloud", False)) is not bool:
            return invalid("scope allow_cloud must be boolean")
        # ADR-0060: the scope names who may write into it. Empty or absent means nobody; a client's write grant alone is
        # not enough (config.py refuses a grant whose client is not listed here, memory.py checks it again per request).
        writers = client_names(spec.get("writers", []))
        if writers is None:
            return invalid(f"memory_scopes.{alias}.writers must be a list of at most 64 distinct client names")
        # ADR-0145: writes of a writer that is not a reviewer wait in the outbox until a reviewer approves them.
        review = spec.get("review", False)
        if type(review) is not bool:
            return invalid(f"memory_scopes.{alias}.review must be boolean")
        reviewers = client_names(spec.get("reviewers", []))
        if reviewers is None:
            return invalid(f"memory_scopes.{alias}.reviewers must be a list of at most 64 distinct client names")
        if review and not reviewers:
            return invalid(f"memory_scopes.{alias}.review needs at least one entry in reviewers")
        if not review and ({"reviewers", "review_expire_days", "review_max_held"} & set(spec)):
            # a review setting without review: true would look like protection that is not there
            return invalid(f"memory_scopes.{alias}: reviewers, review_expire_days and review_max_held need review: true")
        expire_days, max_held = bounded(spec, "review_expire_days", REVIEW_EXPIRE_DAYS), bounded(spec, "review_max_held", REVIEW_MAX_HELD)
        if expire_days is None:
            return invalid(f"memory_scopes.{alias}.review_expire_days must be an integer from 1 to 365")
        if max_held is None:
            return invalid(f"memory_scopes.{alias}.review_max_held must be an integer from 1 to 10000")
        result[alias] = {"data_class": spec["data_class"], "allow_cloud": spec.get("allow_cloud", False), "writers": writers,
                         "review": review, "reviewers": reviewers, "review_expire_days": expire_days,
                         "review_max_held": max_held}
    return result
