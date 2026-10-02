#!/usr/bin/env python3
"""Selbsttest TCP-Keepalive auf dem API-Port: kommen die Optionen des lauschenden Sockets auf der ANGENOMMENEN Verbindung an?
Laeuft ohne Router, nur ueber Loopback mit einer einzigen Verbindung.

Anlass 2026-10-01: eine Anfrage ohne Stream rechnete 6,6 min ohne ein Byte, der Verbindungszustand auf dem Weg zum Client
verfiel still, die Antwort ging verloren. Der Router setzt Keepalive am lauschenden Socket und verlaesst sich darauf, dass Linux
die Optionen an jede angenommene Verbindung vererbt - genau das prueft dieser Test, statt es anzunehmen.
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
