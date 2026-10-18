"""Shared persistence errors and public record conversion."""
from uuid import UUID


class SessionNotFound(Exception):
    pass


class SessionConflict(Exception):
    pass


class SessionUnavailable(Exception):
    pass


class SessionCapacity(Exception):
    pass


def record(row):
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
    return result
