"""Security posture of a loaded configuration as facts (TM-01, TM-11; ADR-0064).

`router.py --posture <config.yaml>` and the start of the router report what the configuration allows that the threat
model calls risky: an API port that serves unknown callers, a client policy changeable from the UI, clients recognised
only by their source address or sharing one token, a control port open without login, plaintext transport to a node's
Ollama, the session store, or an API port without TLS. Each finding names its code, severity, the risk it maps to and
the next step; no secret, token, hash or connection string is printed. The check reads the configuration and, for the
session store, the sslmode of the DSN in the environment (never the DSN itself).

Findings are facts, not errors: the router still starts. A finding the operator accepts stays visible on every start.
"""
import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

HIGH, MEDIUM, INFO = "high", "medium", "info"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    risk: str
    text: str
    next_step: str

    def line(self):
        return f"{self.severity.upper():6} {self.code} ({self.risk}): {self.text} Next: {self.next_step}"


def _loopback(host):
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:  # exception-ok: ip_address is the only full validity check; a host name is simply not a literal IP
        return False


def findings(cfg, environ=None):
    environ = os.environ if environ is None else environ
    out = []
    ca = cfg.client_auth or {}
    clients = ca.get("clients") or {}
    # TM-01: who may call the API port, and how the policy can change
    if ca.get("mode") != "enforce":
        out.append(Finding("client_auth_observe", HIGH, "TM-01", "client_auth.mode is observe: callers without identity are served.",
                           "set router.client_auth.mode: enforce"))
    if not ca.get("locked"):
        out.append(Finding("client_auth_unlocked", MEDIUM, "TM-01", "client_auth.locked is false: the mode can be switched in the UI.",
                           "set router.client_auth.locked: true"))
    for name, spec in sorted(clients.items()):
        if spec.get("ip") and not spec.get("token_sha256"):
            # With inference: false the address only opens /rerank and /info (auth.NON_INFERENCE_ALLOWED).
            limited = spec.get("inference") is False
            out.append(Finding("client_ip_only", INFO if limited else MEDIUM, "TM-01",
                               f"client {name} is recognised only by its source address"
                               + (" (inference: false, rerank only)." if limited else "."),
                               f"give {name} a token, or keep the address range minimal and accept the risk"))
    by_hash = {}
    for name, spec in clients.items():
        if spec.get("token_sha256"):
            by_hash.setdefault(spec["token_sha256"], []).append(name)
    for names in sorted(v for v in by_hash.values() if len(v) > 1):
        out.append(Finding("clients_share_token", HIGH, "TM-01", f"clients {', '.join(sorted(names))} share one token.",
                           "rotate the token of all but one (POST /admin/clients/<name>/token)"))
    if not getattr(cfg, "control_users", None):
        out.append(Finding("control_without_users", HIGH, "TM-03", "control_auth has no users: the control port answers 503.",
                           "add router.control_auth.users"))
    if getattr(cfg, "control_open_loopback", False):
        out.append(Finding("control_open_loopback", MEDIUM, "TM-03", "control port is open without login from loopback.",
                           "remove router.control_auth.allow_unauthenticated_loopback (development only)"))
    if not getattr(cfg, "api_tls", False):
        out.append(Finding("api_without_tls", MEDIUM, "TM-01", "API port serves plain HTTP: tokens cross the network unencrypted.",
                           "set router.api_tls: true with control_tls certificates"))
    # TM-11: transport to the backends the router talks to directly
    for name, spec in sorted((getattr(cfg, "nodes", None) or {}).items()):
        url = urlsplit(str(spec.get("ollama") or ""))
        if url.scheme == "http" and not _loopback(url.hostname or "") and not spec.get("ollama_tls_fingerprint"):
            out.append(Finding("node_plain_http", MEDIUM, "TM-11", f"static node {name} is reached over plain HTTP.",
                               "use an agent (tunnel) or HTTPS with ollama_tls_fingerprint"))
    store = getattr(cfg, "session_store", None)
    if store is not None and getattr(store, "enabled", False):
        dsn = environ.get(store.dsn_env, "")
        if dsn:
            query = parse_qs(urlsplit(dsn).query) if "://" in dsn else {}
            mode = (query.get("sslmode") or [""])[0]
            host = urlsplit(dsn).hostname or "" if "://" in dsn else ""
            if mode not in ("verify-ca", "verify-full") and not _loopback(host):
                out.append(Finding("session_store_unverified_tls", MEDIUM, "TM-11",
                                   f"session store DSN uses sslmode={mode or 'default'} to a non-loopback host.",
                                   "use sslmode=verify-full (or verify-ca) with sslrootcert"))
        else:
            out.append(Finding("session_store_dsn_unset", INFO, "TM-11", f"session store enabled, {store.dsn_env} not set here.",
                               "run the posture check where the service environment is loaded"))
    return out


def report(cfg, environ=None):
    """(lines, summary) for the CLI and the start log."""
    found = findings(cfg, environ)
    order = {HIGH: 0, MEDIUM: 1, INFO: 2}
    lines = [f.line() for f in sorted(found, key=lambda f: (order[f.severity], f.code))]
    counts = {s: sum(1 for f in found if f.severity == s) for s in (HIGH, MEDIUM, INFO)}
    summary = "POSTURE: ok" if not found else f"POSTURE: {counts[HIGH]} high, {counts[MEDIUM]} medium, {counts[INFO]} info"
    return lines, summary
