"""Werte aus fremder Eingabe (UI, Admin-API) pruefen: der Wert oder ein Problem 400 mit Klartext.

Die einzigen Stellen, an denen die Referenzparser fuer Zahlentext und IP-Adressen ihr ValueError werfen duerfen; alle anderen
Pruefungen sind ausdrueckliche Tests (AGENTS.md "Explicit control flow")."""
import ipaddress
import math

from .outcome import invalid


def number(value, what, *, integer=False):
    """Zahl aus int, float oder Zahlentext (1e3, -0.5); bool zaehlt nicht, unendlich und NaN auch nicht. integer=True verlangt
    eine ganze Zahl (3.0 ja, 3.5 nein) und liefert int."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return invalid(f"{what}: Zahl erwartet")
    if isinstance(value, int):
        return value if integer else float(value)
    try:
        v = float(value)
    except ValueError:  # exception-ok: float() is the reference parser for a numeric string (1e3, -0.5, ...)
        return invalid(f"{what}: Zahl erwartet")
    if not math.isfinite(v):
        return invalid(f"{what}: endliche Zahl erwartet")
    if integer:
        if v != int(v):
            return invalid(f"{what}: ganze Zahl erwartet")
        return int(v)
    return v


def ip_address(value, what):
    """IPv4/IPv6-Adresse in Normalform."""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:  # exception-ok: ipaddress is the reference parser for IPv4/IPv6 text
        return invalid(f"{what}: {value!r} ist keine IP-Adresse")
