"""Wake-on-LAN: the magic packet of the built-in `wol` waker (wakers.py, ADR-0117)."""

import socket

from . import state


def send_magic_packet(mac):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:   # closed also when a send fails
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in state.CFG.wol_broadcasts:
            s.sendto(pkt, (addr, 9))
