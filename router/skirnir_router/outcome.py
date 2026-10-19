"""Expected outcomes are values, not exceptions (AGENTS.md, "Explicit control flow").

Code that detects an expected condition (invalid input, missing grant, unknown
session, conflicting event, full queue, unavailable backend) returns a Problem
with the HTTP status and a fixed, safe message chosen where it is detected.
Callers test `failed(value)` and pass the problem on; nobody parses message
text or infers state from an exception type. Exceptions remain for faults that
cannot be checked beforehand (database, network, OS, cancellation) and are
caught only at those boundaries, where they become a Problem.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Problem:
    status: int
    message: str
    kind: str = ""   # fixed diagnostic category (outbox last_error), never free text


def failed(value):
    return isinstance(value, Problem)


def invalid(message):
    return Problem(400, message)


def unauthenticated(message):
    return Problem(401, message)


def denied(message):
    return Problem(403, message)


def not_found(message):
    return Problem(404, message)


def conflict(message):
    return Problem(409, message)


def too_large(message):
    return Problem(413, message)


def bad_gateway(message):
    return Problem(502, message)


def unavailable(message, kind="unavailable"):
    return Problem(503, message, kind)
