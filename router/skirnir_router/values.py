"""Validate values from external input (UI, Admin API): return the value or a Problem 400 with plain text.

The only places where the reference parsers for numeric text and IP addresses may raise ValueError; all other
checks are explicit tests (AGENTS.md "Explicit control flow")."""
import ipaddress
import math

from .outcome import invalid


def number(value, what, *, integer=False):
    """Number from int, float, or numeric text (1e3, -0.5); bool does not count, nor do infinity and NaN. integer=True requires
    an integer (3.0 yes, 3.5 no) and returns int."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return invalid(f"{what}: number expected")
    if isinstance(value, int):
        return value if integer else float(value)
    try:
        v = float(value)
    except ValueError:  # exception-ok: float() is the reference parser for a numeric string (1e3, -0.5, ...)
        return invalid(f"{what}: number expected")
    if not math.isfinite(v):
        return invalid(f"{what}: finite number expected")
    if integer:
        if v != int(v):
            return invalid(f"{what}: whole number expected")
        return int(v)
    return v


def ip_address(value, what):
    """IPv4/IPv6-Adresse in Normalform."""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:  # exception-ok: ipaddress is the reference parser for IPv4/IPv6 text
        return invalid(f"{what}: {value!r} is not an IP address")
