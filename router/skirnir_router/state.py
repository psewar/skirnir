"""Shared runtime state of the router. Always access as `state.X` - CFG, SESSION, REG, and
HA_PUB are re-bound at startup; `from .state import CFG` would see the old value."""

import asyncio
import time


# The agent authenticates with an Ed25519 key (signature over a nonce). Unknown keys land
# as "awaiting approval" in the registry (nodes.json) and are approved in the UI; this sets Policy (WOL,
# weight, MQTT). Approved nodes receive their config package (MQTT access, intervals) via the tunnel.
REG = None
PENDING = {}    # fp -> tunnel, connected but not approved
HA_PUB = None


CFG = None
NODES = {}
MQTT_DIRTY = []   # non-empty = publish immediately
TASKS = set()     # Background tasks (spawn): the loop holds only weak references; an unreferenced task may be GC'd before completion
MQTT_EVENTS = []  # HA events for ha.py to send once on the next pass (not retained)
TRUNCATIONS = {"count": 0, "last": None}   # confirmed silent truncations since router start (context_check.py)
MEASURING = {}    # model -> {"node", "step", "started", "error"} during an active measurement
PERF = {}         # "model@node" -> performance data (passive from real requests + active benchmark), persisted in perf.json
PERF_DIRTY = [0.0]


BENCHING = {}     # model -> {"node","step","started","error"}
SESSION = None
DECISIONS = []   # Ring buffer of recent routing decisions and events (DECISIONS_KEEP entries, ~300 B per entry)
DECISIONS_KEEP = 2000   # Operator 2026-09-25: 50 in the UI was too few; a night's worth (guard state sequence) was already gone
DECISIONS_FILE = None   # events.jsonl next to config.yaml (decisions_load sets it; None = no persistence)
_DEC_APPENDED = 0       # Lines since last compaction
_DEC_WARNED = 0.0       # Last 'write failed' warning (at most every 10 min)
CAPS = {}        # model -> capabilities (from /api/show)
SESSIONS = {}    # routing.session_id -> {node, model, t} (affinity, stage 1)
CLOUD = {}       # Stage 5: provider -> cloud.CloudTarget
METRICS = {"counters": {}, "hist": {}}   # Stage 4: Prometheus counters/histograms (metrics.py); gauges are live; persisted in metrics.json
METRICS_DIRTY = [0.0]                    # Timestamp of first unsaved change (tick_loop saves after METRICS_SAVE_S)
USAGE = {"days": {}}                     # Stage 4: usage per day/client/role/model, persisted in usage.json
USAGE_DIRTY = [0.0]
# Client authentication (Stage 2): runtime override of mode (POST /admin/client_auth, valid until restart;
# persistent config is config.yaml) and counters for the observation phase - who comes with which identity, who without.
CLIENT_AUTH_MODE = None
CLIENT_STATS = {"clients": {}, "unauth": {}}
# Router's own identity for UI self-calls ("Try it", admin.handle_try): random at start, in-memory only,
# valid from localhost only. Without this, the button hit 401 in enforce mode (Operator, 2026-09-10). Name: skirnir-ui.
INTERNAL_TOKEN = None
INTERNAL_CLIENT = "skirnir-ui"


def spawn(coro):
    """asyncio.create_task with holder: reference in TASKS until completion (RUF006, Python docs on create_task)."""
    t = asyncio.get_running_loop().create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


def decisions_path():
    import os
    return os.path.join(os.path.dirname(os.path.abspath(CFG.path)), "events.jsonl") if CFG is not None and getattr(CFG, "path", None) else None


def decisions_load():
    """At startup: load the last DECISIONS_KEEP entries from events.jsonl into the ring and compact the file.
    This ensures the log survives restarts and deploys (Operator 2026-09-25: the night was gone after deploy)."""
    global DECISIONS_FILE, _DEC_APPENDED
    import os
    from .common import log
    DECISIONS_FILE = decisions_path()
    if not DECISIONS_FILE or not os.path.exists(DECISIONS_FILE):
        return 0
    try:
        with open(DECISIONS_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, ValueError) as e:  # exception-ok: file system and the file's encoding (UnicodeDecodeError)
        log.warning("events.jsonl unreadable: %s", e)
        return 0
    rows = [r for r in map(_journal_row, lines) if r is not None]
    rows = rows[-DECISIONS_KEEP:]
    DECISIONS[:0] = rows
    del DECISIONS[:-DECISIONS_KEEP]
    _decisions_compact()
    return len(rows)


# Names in events.jsonl prior to Router 0.9.0 (design/english-names.md): translated on load so filters find old and new
# entries equally. Keys: field -> {old: new}; plus fields of the truncation event.
JOURNAL_BEFORE_0_9 = {
    "event": {"kontext_gekuerzt": "context_truncated"},
    "reason": {"spiel-leicht": "game_light", "spiel-vorbei": "game_ended", "ausweichen": "fallback", "abgewaehlt": "deselected"},
    "state": {"aus": "off", "unverfuegbar": "unavailable", "hochlast": "high_load", "gedrosselt": "throttled", "erholung": "recovery"},
    "status": {"cloud uebersprungen": "cloud_skipped", "kein Knoten": "no_node"},
}
TRUNCATION_FIELDS_BEFORE_0_9 = {"geschaetzt": "estimated", "angekommen": "received", "quote": "ratio", "sicher": "certain"}


def _journal_english(r):
    """An entry from events.jsonl with names from Router 0.9.0 onwards."""
    for field, names in JOURNAL_BEFORE_0_9.items():
        if r.get(field) in names:
            r[field] = names[r[field]]
    if r.get("event") == "context_truncated":
        r = {TRUNCATION_FIELDS_BEFORE_0_9.get(k, k): v for k, v in r.items()}
    return r


def _journal_row(line):
    """A line from events.jsonl as an entry (names from 0.9.0 onwards), None for empty lines and truncated lines (crash during write)."""
    import json
    if not line.strip():
        return None
    try:
        r = json.loads(line)
    except ValueError:  # exception-ok: a line torn by a crash while writing; whether it parses shows only the parser
        return None
    return _journal_english(r) if isinstance(r, dict) and r.get("event") and r.get("t") else None


def _decisions_compact():
    """Rewrite the file with the current ring contents (atomic via .tmp)."""
    global _DEC_APPENDED
    import json
    import os
    if not DECISIONS_FILE:
        return
    tmp = DECISIONS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for e in DECISIONS:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, DECISIONS_FILE)
    except (OSError, TypeError, ValueError) as e:  # exception-ok: file system boundary; json reports unencodable entries only so
        _warn_journal(e)
        return
    _DEC_APPENDED = 0


def _warn_journal(e):
    """Throttle journal write errors (at most every 10 min); the in-memory log continues."""
    global _DEC_WARNED
    if time.time() - _DEC_WARNED > 600:
        _DEC_WARNED = time.time()
        from .common import log
        log.warning("events.jsonl write: %s", e)


def decisions_append(entry):
    """Append a line; compact after 4 x DECISIONS_KEEP lines to prevent unbounded growth
    (~300 B per entry: max ~2.5 MB). Write errors are throttled; the in-memory log continues."""
    global _DEC_APPENDED
    if not DECISIONS_FILE:
        return
    import json
    try:
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as e:  # exception-ok: file system boundary; json reports unencodable entries only so
        _warn_journal(e)
        return
    _DEC_APPENDED += 1
    if _DEC_APPENDED >= 4 * DECISIONS_KEEP:
        _decisions_compact()


def remember(entry):
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry["t"] = time.time()
    if entry.get("event") in ("busy", "free", "wol", "no_node", "context_truncated"):
        MQTT_DIRTY.append(True)
    DECISIONS.append(entry)
    del DECISIONS[:-DECISIONS_KEEP]
    decisions_append(entry)
    from . import metrics   # late, to avoid import cycles
    metrics.count_event(entry)
