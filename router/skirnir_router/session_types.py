"""Shared persistence outcomes, the database boundary and public record conversion."""
import functools
from uuid import UUID

from .outcome import not_found, unavailable

NOT_FOUND = not_found("session not found")
UNAVAILABLE = unavailable("session store unavailable; retry with the same event_id")


def database(method):
    """Database boundary of every public store method. Driver, network and timeout faults cannot be
    checked beforehand; they are caught here and nowhere else. Driver messages may contain DSNs or
    secrets, so only the fixed UNAVAILABLE problem leaves the store. Cancellation passes through."""
    @functools.wraps(method)
    async def call(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except self.driver_errors:  # exception-ok: the database boundary (see docstring)
            return UNAVAILABLE
    return call


class Transaction:
    """One database transaction. A method that detects an expected problem returns
    `tx.reject(problem)`; the transaction then rolls back instead of committing."""
    def __init__(self, conn):
        self.conn, self.problem = conn, None

    def reject(self, problem):
        self.problem = problem
        return problem


def record(row):
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
    return result
