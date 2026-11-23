"""Waking offline nodes through replaceable wakers (ADR-0117, transport rules ADR-0124).

A waker only *sends* the wake signal and reports that as a fact: `wake(node)` returns `Sent` or a `Problem` with a fixed
`kind`. Whether the machine actually comes up is judged in one place, the wait-and-poll loop in `wake()` below, exactly
as with Wake-on-LAN before: poll every 2 s until `router.wol.wait_up_s`, cooldown `router.wol.cooldown_s`.

Kinds:
  wol           built in, the default: UDP magic packet to `router.wol.broadcast_addresses` (wol.py), repeated every
                `router.wol.retry_interval_s` while waiting. Needs the node's MAC.
  http_webhook  POST of a fixed JSON body {"event": "wake", "node", "mac"} to the configured final URL (a Home Assistant,
                ioBroker or openHAB webhook, a smart plug's controller). HTTPS unless literal loopback, no redirect, a
                total timeout, optional bearer token from an environment variable. Sent once per attempt unless `repeat`.
There is deliberately no command waker: the router runs unprivileged and executes nothing configured (ADR-0118, ADR-0124).
"""
import asyncio
import os
import time
from dataclasses import dataclass
from typing import Protocol

import aiohttp
from yarl import URL

from . import poll, state, wol
from .common import VERSION, log, normalize_mac
from .outcome import Problem, failed
from .waker_config import BUILTIN


@dataclass(frozen=True)
class Sent:
    """The wake signal left the router (not: the node is awake)."""
    waker: str
    detail: str    # what was sent, safe for the log (MAC, host; never a URL path or a token)


class Waker(Protocol):
    """What the wait-and-poll loop needs from a waker. Implementations live in this repository and are chosen by
    configuration; there are no plug-ins (ADR-0118)."""
    name: str      # configured name ("wol" for the built-in one)
    kind: str      # "wol" | "http_webhook"
    repeat: bool   # send again every router.wol.retry_interval_s while waiting

    def ready(self, node) -> bool:
        """Does the node have what this waker needs (e.g. a MAC)?"""

    async def wake(self, node) -> "Sent | Problem":
        """Send the wake signal once; report the fact, never raise for an expected failure."""


class WolWaker:
    name = kind = BUILTIN
    repeat = True

    def ready(self, node):
        return bool(node.mac)

    async def wake(self, node):
        mac = normalize_mac(node.mac)
        if mac is None:   # from facts or old policy; otherwise bytes.fromhex() would result in a 500 on the client
            return Problem(503, f"no valid MAC ({node.mac!r})", "wake_no_mac")
        try:
            wol.send_magic_packet(mac)
        except OSError as e:  # exception-ok: OS socket boundary (no route, interface down); reported as the waker's fact
            return Problem(503, f"magic packet not sent: {e.__class__.__name__}", "wake_send_failed")
        return Sent(self.name, mac)


class WebhookWaker:
    kind = "http_webhook"

    def __init__(self, spec):
        self.spec = spec
        self.name = spec.name
        self.repeat = spec.repeat
        self.host = URL(spec.url).host   # parsed and checked at configuration load (values.egress_url)

    def ready(self, node):
        return True

    async def wake(self, node):
        headers = {"Content-Type": "application/json", "User-Agent": f"skirnir-router/{VERSION}"}
        if self.spec.token_env:
            token = os.environ.get(self.spec.token_env) or ""
            if not token:   # fail closed: a configured token that is missing never becomes an unauthenticated call
                return Problem(503, f"environment variable {self.spec.token_env} is empty", "wake_secret_missing")
            headers["Authorization"] = f"Bearer {token}"
        body = {"event": "wake", "node": node.name, "mac": normalize_mac(node.mac)}
        timeout = aiohttp.ClientTimeout(total=self.spec.timeout_s)
        try:
            async with state.SESSION.post(self.spec.url, json=body, headers=headers, timeout=timeout,
                                          allow_redirects=False) as r:
                status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:  # exception-ok: network boundary, timeout included
            return Problem(503, f"{self.host} not reached: {e.__class__.__name__}", "wake_unreachable")
        if 300 <= status < 400:   # ADR-0048/0124: never follow; the operator enters the final URL
            return Problem(502, f"{self.host} answered with redirect {status}; configure the final URL", "wake_redirect")
        if not 200 <= status < 300:
            return Problem(502, f"{self.host} answered HTTP {status}", "wake_refused")
        return Sent(self.name, f"{self.host} HTTP {status}")


WOL = WolWaker()


def waker_for(node):
    """The node's waker, or a Problem when its name is not (any longer) configured."""
    name = node.waker or BUILTIN
    if name == BUILTIN:
        return WOL
    spec = state.CFG.wakers.get(name)
    if spec is None:
        return Problem(503, f"waker {name!r} is not configured in router.wakers", "wake_unknown_waker")
    return WebhookWaker(spec)


def can_wake(node):
    """May and can the router try to wake this node (policy wol, a configured waker with what it needs)?"""
    if not node.wol:
        return False
    waker = waker_for(node)
    return not failed(waker) and waker.ready(node)


async def wake(node):
    """Wakes a node and waits for Ollama to respond. True on success. Parallel requests share one attempt."""
    async with node.wake_lock:
        if node.state != "offline":
            return True
        now = time.time()
        if now - node.last_wake < state.CFG.wol_cooldown_s:
            return False
        node.last_wake = now
        waker = waker_for(node)
        sent = waker if failed(waker) else await waker.wake(node)
        if failed(sent):
            log.warning("wake %s via %s: %s (%s)", node.name, node.waker, sent.message, sent.kind)
            return False
        log.info("%s %s (%s)", "WOL" if waker.kind == BUILTIN else f"wake via {waker.name}", node.name, sent.detail)
        state.remember({"event": "wol", "node": node.name, "waker": waker.name})
        deadline = now + state.CFG.wol_wait_s
        last_send = now
        while time.time() < deadline:
            await asyncio.sleep(2)
            await poll.poll_node(node)
            if node.state != "offline":
                log.info("node %s awake after %.0fs", node.name, time.time() - now)
                return True
            if waker.repeat and time.time() - last_send >= state.CFG.wol_retry_s:
                again = await waker.wake(node)
                if failed(again):
                    log.warning("wake %s via %s: repeat: %s (%s)", node.name, waker.name, again.message, again.kind)
                last_send = time.time()
        log.warning("node %s did not wake within %.0fs", node.name, state.CFG.wol_wait_s)
        return False
