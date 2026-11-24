"""Configuration of the named wakers in `router.wakers` (ADR-0117, ADR-0124). Returns the value or a Problem (outcome.py).

A waker is how the router switches on an offline node before a request. `wol` (Wake-on-LAN, `router.wol`) is built in and
the default; every other waker is defined here by name and chosen per node with `nodes.<name>.waker` or the node policy.
URLs and secret references live only in the configuration file: the web UI may pick a configured name, never a destination.
"""
import math
import re
from dataclasses import dataclass

from . import values
from .outcome import failed, invalid

BUILTIN = "wol"
KINDS = ("http_webhook",)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


@dataclass(frozen=True)
class WebhookSpec:
    name: str
    url: str                 # final URL, checked with values.egress_url
    token_env: str = ""      # environment variable with a bearer token; empty = no Authorization header
    timeout_s: float = 10.0  # the whole exchange; a slow receiver never holds the wake loop longer
    repeat: bool = False     # send again every router.wol.retry_interval_s while waiting (a toggle must not repeat)
    kind: str = "http_webhook"


FIELDS = ("kind", "url", "token_env", "timeout_s", "repeat")


def parse_wakers(raw):
    """{name: WebhookSpec} from router.wakers, or a Problem 400."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return invalid("router.wakers must map names to waker definitions")
    out = {}
    for name, spec in raw.items():
        where = f"router.wakers.{name}"
        if not isinstance(name, str) or not NAME_RE.match(name):
            return invalid(f"{where}: name must be 1-63 letters, digits, '.', '_' or '-'")
        if name == BUILTIN:
            return invalid(f"{where}: '{BUILTIN}' is the built-in Wake-on-LAN waker (configured in router.wol)")
        if not isinstance(spec, dict) or set(spec) - set(FIELDS):
            return invalid(f"{where}: expected the keys {list(FIELDS)}")
        if spec.get("kind") not in KINDS:
            return invalid(f"{where}.kind must be one of {list(KINDS)}")
        url = values.egress_url(spec.get("url"), f"{where}.url")
        if failed(url):
            return url
        token_env = spec.get("token_env") or ""
        if not isinstance(token_env, str) or (token_env and not ENV_RE.match(token_env)):
            return invalid(f"{where}.token_env must name an environment variable")
        timeout = spec.get("timeout_s", WebhookSpec.timeout_s)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
            return invalid(f"{where}.timeout_s must be between 1 and 60")
        repeat = spec.get("repeat", False)
        if type(repeat) is not bool:
            return invalid(f"{where}.repeat must be true or false")
        out[name] = WebhookSpec(name=name, url=str(url), token_env=token_env, timeout_s=float(timeout), repeat=repeat)
    return out


def check_choice(choice, wakers, where):
    """None if `choice` names the built-in or a configured waker, otherwise a Problem 400."""
    if choice is None or choice == BUILTIN or (isinstance(choice, str) and choice in wakers):
        return None
    return invalid(f"{where}: unknown waker {choice!r}; choose '{BUILTIN}' or one of router.wakers {sorted(wakers)}")
