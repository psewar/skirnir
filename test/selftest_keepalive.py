#!/usr/bin/env python3
"""Self-test TCP keepalive on the API port: do the listening socket's options reach the ACCEPTED connection?
Runs without a router, only over loopback with a single connection.

Trigger 2026-10-01: a non-stream request computed for 6.6 min without a byte, the connection state on the way to the client
quietly expired, and the response was lost. The router sets keepalive on the listening socket and relies on Linux
inheriting the options to every accepted connection - this test verifies that instead of assuming it.
"""
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import app  # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def main():
    srv = app.keepalive_socket("127.0.0.1:0")
    port = srv.getsockname()[1]
    cli = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn, _ = srv.accept()
    try:
        check("lauschender Socket: SO_KEEPALIVE an", srv.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0)
        check("angenommene Verbindung: SO_KEEPALIVE an (geerbt)", conn.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0)
        for name, wert in app.KEEPALIVE:
            if not hasattr(socket, name):
                print(f"SKIP {name}: gibt es auf {sys.platform} nicht")
                continue
            ist = conn.getsockopt(socket.IPPROTO_TCP, getattr(socket, name))
            check(f"angenommene Verbindung: {name} = {wert} (geerbt)", ist == wert, f"ist {ist}")
        if sys.platform.startswith("linux"):
            check("auf Linux sind alle TCP_KEEP*-Optionen vorhanden (der Router laeuft auf Linux)",
                  all(hasattr(socket, n) for n, _ in app.KEEPALIVE))
    finally:
        conn.close()
        cli.close()
        srv.close()
    print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
