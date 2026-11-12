"""Deletion journal (ADR-0076, TM-12): deletions survive a restore of the database that held the data.

Every completed deletion of a session or a memory operation through the API is appended - ids only, no content - to
deletions.jsonl next to config.yaml, outside the PostgreSQL database. When the session store opens, before the memory
outbox sends anything, the journal is applied to the database again (PostgresSessionStore.replay_deletions): after a
restore of an older database backup, deleted sessions are deleted again and deleted memory operations are marked again,
so restored pending sends of deleted knowledge never leave. Retention deletions are not journaled; the same retention
rule removes restored expired sessions again.
"""
import json
import os
import re
import time
from uuid import UUID

from . import state
from .common import log, open_private, state_dir
from .outcome import failed, unavailable

KINDS = ("session", "operation")
UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_warned = [False]


def path():
    cfg_path = getattr(state.CFG, "path", None) if state.CFG is not None else None
    return os.path.join(state_dir(state.CFG), "deletions.jsonl") if cfg_path else None


def record(kind, owner, ident):
    """Append one completed deletion. A write failure is logged once; the deletion itself stands."""
    p = path()
    if p is None or kind not in KINDS:
        return
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "owner": owner, "id": str(ident)})
    try:
        with open_private(p, "a") as f:
            f.write(line + "\n")
    except OSError as e:  # exception-ok: file system boundary; the database deletion already happened
        if not _warned[0]:
            log.warning("deletion journal %s not writable (%s) - deletions would not survive a database restore", p, e)
            _warned[0] = True


def parse(lines):
    """(sessions, operations, skipped): lists of (owner, UUID) from journal lines; malformed lines are counted, not fatal."""
    sessions, operations, skipped = [], [], 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:  # exception-ok: a line torn by a crash mid-write cannot be recognised before parsing
            row = None
        ident = row.get("id") if isinstance(row, dict) else None
        valid = (isinstance(row, dict) and row.get("kind") in KINDS and isinstance(row.get("owner"), str) and row["owner"]
                 and isinstance(ident, str) and UUID_TEXT.match(ident.lower()) is not None)
        if not valid:
            skipped += 1
            continue
        (sessions if row["kind"] == "session" else operations).append((row["owner"], UUID(ident)))
    return list(dict.fromkeys(sessions)), list(dict.fromkeys(operations)), skipped


async def replay(store, with_memory):
    """Apply the journal to the store. The counts, or a Problem from the database."""
    p = path()
    if p is None or not os.path.exists(p):
        return {"sessions": 0, "operations": 0}
    try:
        with open(p, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
    except (OSError, ValueError) as e:  # exception-ok: file system and encoding; without the journal nothing may be sent
        log.error("deletion journal %s unreadable: %s", p, e)
        return unavailable("deletion journal unreadable")
    sessions, operations, skipped = parse(lines)
    result = await store.replay_deletions(sessions, operations, with_memory)
    if failed(result):
        return result
    if skipped:
        log.warning("deletion journal: %d malformed line(s) ignored", skipped)
    if result["sessions"] or result["operations"]:
        log.warning("deletion journal: %d session(s) and %d memory operation(s) were back in the database and are deleted "
                    "again - was the database restored from a backup? (ADR-0076)", result["sessions"], result["operations"])
    else:
        log.info("deletion journal: %d session and %d operation deletion(s) checked, none to repeat", len(sessions), len(operations))
    return result
