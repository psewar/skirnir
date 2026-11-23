#!/usr/bin/env python3
"""Health probe of the router image (ADR-0116): GET /api/version on the router's API port, from inside the container.

    python healthcheck.py [config.yaml]        default /etc/skirnir-router/config.yaml; exit 0 healthy, 1 not

Port and scheme come from the same config.yaml the router reads (router.listen; https when router.api_tls is set and
control_tls names a certificate, as the router decides). A wildcard listen address is probed on loopback. /api/version
needs no identity (auth.EXEMPT_PATHS), so the probe sends nothing secret; it does not verify the certificate, because
the name in it is the public one, not the loopback address. Last line: HEALTH: ok <version> | HEALTH: failed <reason>.
Standard library and PyYAML only; it runs as a plain file and imports nothing from the router.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

import yaml

DEFAULT_CONFIG = "/etc/skirnir-router/config.yaml"
TIMEOUT_S = 4
WILDCARDS = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1", "[::]": "::1"}


def target(cfg):
    """(url, tls) of /api/version for a loaded configuration, or (None, reason) when it names no usable port."""
    router = cfg.get("router") if isinstance(cfg, dict) else None
    router = router if isinstance(router, dict) else {}
    listen = str(router.get("listen") or "0.0.0.0:11434")
    host, _, port = listen.rpartition(":")
    if not port.isdigit() or not 0 < int(port) < 65536:
        return None, f"router.listen {listen!r} has no port"
    host = WILDCARDS.get(host, host.strip("[]"))
    tls_files = router.get("control_tls") if isinstance(router.get("control_tls"), dict) else {}
    tls = bool(router.get("api_tls")) and bool(tls_files.get("cert")) and bool(tls_files.get("key"))
    shown = f"[{host}]" if ":" in host else host
    return f"{'https' if tls else 'http'}://{shown}:{int(port)}/api/version", tls


def load(path):
    """(configuration dict, None) or (None, reason)."""
    if not os.path.isfile(path):
        return None, f"{path} not found (mount the configuration folder, or write one with --init)"
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:  # exception-ok: file system and the operator's YAML are boundaries
        return None, f"{path} cannot be read: {type(exc).__name__}"
    return (cfg, None) if isinstance(cfg, dict) else (None, f"{path} is not a mapping")


def probe(url, tls, timeout=TIMEOUT_S):
    """(True, version) or (False, reason)."""
    context = None
    if tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as r:
            status, body = r.status, r.read(4096)
    except urllib.error.HTTPError as exc:  # exception-ok: urllib reports a non-2xx answer only as an exception
        return False, f"{url} answered {exc.code}"
    except (OSError, ssl.SSLError) as exc:  # exception-ok: network boundary (refused, timeout, TLS)
        return False, f"{url} unreachable: {type(exc).__name__}"
    try:
        answer = json.loads(body)
    except ValueError:  # exception-ok: parsing the answer of a process that may not be the router
        answer = None
    version = answer.get("version") if isinstance(answer, dict) else None
    if status != 200 or not isinstance(version, str):
        return False, f"{url} answered {status} without a version"
    return True, version


def main(argv):
    path = argv[1] if len(argv) > 1 else DEFAULT_CONFIG
    cfg, reason = load(path)
    if cfg is None:
        print(f"HEALTH: failed {reason}")
        return 1
    url, tls = target(cfg)
    if url is None:
        print(f"HEALTH: failed {tls}")
        return 1
    ok, detail = probe(url, tls)
    print(f"HEALTH: {'ok' if ok else 'failed'} {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
