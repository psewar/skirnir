"""Wake-on-LAN."""

import asyncio
import socket
import time

from . import poll, state
from .common import log, normalize_mac


def send_magic_packet(mac):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + raw * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for addr in state.CFG.wol_broadcasts:
        s.sendto(pkt, (addr, 9))
    s.close()


async def wake(node):
    """Weckt einen Knoten und wartet bis Ollama antwortet. True bei Erfolg. Parallel-Anfragen teilen einen Versuch."""
    async with node.wake_lock:
        if node.state != "offline":
            return True
        now = time.time()
        if now - node.last_wake < state.CFG.wol_cooldown_s:
            return False
        node.last_wake = now
        mac = normalize_mac(node.mac)
        if mac is None:   # aus Fakten oder alter Policy; sonst wuerde bytes.fromhex() hier als 500 beim Client landen
            log.warning("WOL %s: keine gueltige MAC (%r)", node.name, node.mac)
            return False
        log.info("WOL %s (%s)", node.name, mac)
        state.remember({"event": "wol", "node": node.name})
        send_magic_packet(mac)
        deadline = now + state.CFG.wol_wait_s
        last_send = now
        while time.time() < deadline:
            await asyncio.sleep(2)
            await poll.poll_node(node)
            if node.state != "offline":
                log.info("node %s awake after %.0fs", node.name, time.time() - now)
                return True
            if time.time() - last_send >= state.CFG.wol_retry_s:
                send_magic_packet(mac)
                last_send = time.time()
        log.warning("node %s did not wake within %.0fs", node.name, state.CFG.wol_wait_s)
        return False
