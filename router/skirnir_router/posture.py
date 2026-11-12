"""Security posture of a loaded configuration as facts (TM-01, TM-11; ADR-0064).

`router.py --posture <config.yaml>` and the start of the router report what the configuration allows that the threat
model calls risky: an API port that serves unknown callers, a client policy changeable from the UI, clients recognised
only by their source address or sharing one token, a control port open without login, plaintext transport to a node's
Ollama, the session store, or an API port without TLS. Each finding names its code, severity, the risk it maps to and
the next step; no secret, token, hash or connection string is printed. The check reads the configuration and, for the
session store, the sslmode of the DSN in the environment (never the DSN itself). At start the router also passes its own
view of the file system (process_view.py, ADR-0083) and reports TLS files it could change; the CLI runs outside the
unit and cannot see that view, so it leaves this check out.

Findings are facts, not errors: the router still starts. A finding the operator accepts stays visible on every start.
"""
import hashlib
import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

HIGH, MEDIUM, INFO = "high", "medium", "info"
# sha256 of placeholder tokens from examples and tutorials; a client still using one has no real secret (ADR-0067).
EXAMPLE_TOKEN_HASHES = frozenset(hashlib.sha256(t.encode()).hexdigest() for t in
                                 ("changeme", "change-me", "secret", "password", "token", "test", "example"))


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


def findings(cfg, environ=None, view=None):
    environ = os.environ if environ is None else environ
    out = []
    # TM-14, ADR-0083: root helpers write the certificates; a file the router could change or replace would let a
    # compromised router steer what they write and where. Only the running router knows its unit's view (view).
    control_tls = getattr(cfg, "control_tls", None)
    if view is not None and control_tls:
        for setting, path in (("control_tls.cert", control_tls[0]), ("control_tls.key", control_tls[1])):
            if view.changeable(path):
                out.append(Finding("tls_files_changeable", MEDIUM, "TM-14",
                                   f"the router could change or replace {setting} ({path}).",
                                   "keep the files in /etc/skirnir-router/certs, which the router unit sees read-only, "
                                   "or make their folder read-only for it (ReadOnlyPaths= in a unit drop-in)"))
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
    for name, spec in sorted(clients.items()):
        if spec.get("token_sha256") in EXAMPLE_TOKEN_HASHES:
            out.append(Finding("client_example_token", HIGH, "TM-01", f"client {name} still has a well-known example token.",
                               f"give {name} its own token (router.py --init, or the Clients tab)"))
    if not getattr(cfg, "control_users", None):
        out.append(Finding("control_without_users", HIGH, "TM-03", "control_auth has no users: the control port answers 503.",
                           "add router.control_auth.users (router.py --hash, or router.py --init)"))
    control_host = str(getattr(cfg, "control_listen", "") or "").rsplit(":", 1)[0]
    if not getattr(cfg, "control_tls", None) and not _loopback(control_host):
        out.append(Finding("control_without_tls", HIGH, "TM-03",
                           "control port without TLS on a network address: admin login and the agents' tunnel are plaintext.",
                           "set router.control_tls (cert, key)"))
    elif not _loopback(control_host):
        out.append(Finding("control_on_network", INFO, "TM-03",
                           "control port listens on a network address (needed for remote agents and the web UI).",
                           "firewall it to your GPU nodes and admin machines"))
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
        # ADR-0076: retention and deletion are choices; the report names the ones that keep content indefinitely
        if not store.retention_days and not store.archived_retention_days:
            out.append(Finding("session_retention_off", INFO, "TM-12", "sessions are kept until their owner deletes them.",
                               "set session_store.retention_days / archived_retention_days if sessions should expire"))
        memory_cfg = getattr(cfg, "hindsight", None)
        if memory_cfg is not None and getattr(memory_cfg, "enabled", False) and not store.delete_memory_with_session:
            out.append(Finding("memory_kept_on_session_delete", INFO, "TM-12",
                               "deleting a session keeps the memory retained from its answers.",
                               "set session_store.delete_memory_with_session: true if it should go with the session"))
        if memory_cfg is not None and getattr(memory_cfg, "outbox_enabled", False) and not getattr(memory_cfg, "outbox_retention_days", 0):
            out.append(Finding("outbox_payload_retention_off", INFO, "TM-12",
                               "finished memory jobs keep their content in the outbox indefinitely.",
                               "set hindsight.outbox_retention_days to drop it after that many days (the job id stays)"))
    return out


def report(cfg, environ=None, view=None):
    """(lines, summary) for the CLI and the start log."""
    found = findings(cfg, environ, view)
    order = {HIGH: 0, MEDIUM: 1, INFO: 2}
    lines = [f.line() for f in sorted(found, key=lambda f: (order[f.severity], f.code))]
    counts = {s: sum(1 for f in found if f.severity == s) for s in (HIGH, MEDIUM, INFO)}
    summary = "POSTURE: ok" if not found else f"POSTURE: {counts[HIGH]} high, {counts[MEDIUM]} medium, {counts[INFO]} info"
    return lines, summary
