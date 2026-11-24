"""Validate values from external input (UI, Admin API): return the value or a Problem 400 with plain text.

The only places where the reference parsers for numeric text and IP addresses may raise ValueError; all other
checks are explicit tests (AGENTS.md "Explicit control flow")."""
import ipaddress
import math

from yarl import URL

from .outcome import invalid

LOOPBACK_HOSTS = ("127.0.0.1", "::1")   # literal loopback only (ADR-0048), never a name such as localhost


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


def egress_url(value, what):
    """An outbound destination the operator configured (ADR-0048 rules, applied to wakers and release documents by
    ADR-0124): the final URL, HTTPS, or plain HTTP only to literal loopback; no credentials, query or fragment.
    Returns the yarl URL or a Problem 400."""
    if not isinstance(value, str) or not value or any(c in value for c in ("?", "#", "\\")) \
            or any(c.isspace() or ord(c) < 32 for c in value):
        return invalid(f"{what}: expected a URL without query or fragment")
    try:
        url = URL(value)
        host = url.raw_host
        port = url.port
    except ValueError:  # exception-ok: yarl is the reference parser for URL authority and port
        return invalid(f"{what}: invalid URL")
    if url.raw_user is not None or url.raw_password is not None or not host or not port:
        return invalid(f"{what}: expected a host without credentials")
    if url.scheme != "https" and not (url.scheme == "http" and host in LOOPBACK_HOSTS):
        return invalid(f"{what}: HTTPS required outside literal loopback")
    return url
