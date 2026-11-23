"""SQLite session store for a single host (ADR-0114, ADR-0122).

The same methods as the PostgreSQL store, on one file in the router's state folder, with the standard library's sqlite3:

* One connection, used only by one worker thread. Every store call holds one asyncio lock for its whole block and runs
  it as one `BEGIN IMMEDIATE` transaction: one writer at a time, which is what PostgreSQL's row locks, advisory locks and
  `SKIP LOCKED` achieve between connections. There is one router process (ADR-0005); `BEGIN IMMEDIATE` and the busy
  timeout also keep a second process (a backup tool, an accidental second router) from interleaving.
* The statements are the PostgreSQL store's own (session_store.CoreStore and the mixins), translated by a fixed rule
  set (`translate`): numbered parameters, the schema prefix, casts, interval arithmetic in microseconds, arrays as JSON
  lists, row locks and advisory locks dropped (the transaction already holds the only write lock). Values cross the
  boundary as the PostgreSQL driver hands them over: UUID, timezone-aware datetime, dict/list, bool.
* Cancellation or a timeout queues the rollback behind the statement still running in the worker thread; the next
  holder's BEGIN runs after it, so no transaction is ever shared.

Methods return their result or a Problem (outcome.py); SQLite and file system faults become UNAVAILABLE at the
`database` boundary.
"""
import asyncio
import functools
import json
import os
import random
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from . import sqlite_migrations
from .common import log
from .exchange_store import ExchangeStore
from .managed_store import ManagedStore
from .outcome import failed, unavailable
from .reliability_store import ReliabilityStore
from .session_store import CoreStore
from .session_types import Transaction, database

# RETURNING (3.35) and STRICT tables (3.37). Debian 12 ships 3.40, Ubuntu 22.04 3.37.
MIN_SQLITE = (3, 37, 0)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MICROS = {"second": 1_000_000, "day": 86_400_000_000}
JSON_COLUMNS = frozenset(("metadata", "payload", "scope", "retry_ids"))
TIME_COLUMNS = frozenset(("created_at", "updated_at", "available_at", "lease_until", "accepted_at"))
UUID_COLUMNS = frozenset(("id", "session_id", "operation_id", "lease"))
BOOL_COLUMNS = frozenset(("delete_requested",))
# Columns PostgreSQL fills with DEFAULT now(); the translation sets them to now() in the INSERT itself (the transaction's
# start on the store clock, as in PostgreSQL), so every timestamp in the file comes from one microsecond clock.
INSERT_TIMES = {"sessions": ("created_at", "updated_at"), "events": ("created_at",), "turns": ("created_at",),
                "compactions": ("created_at",), "memory_outbox": ("created_at", "updated_at", "available_at")}
ADVISORY_LOCK = re.compile(r"\s*SELECT pg_advisory_xact_lock\(.*\)\s*", re.S)
INSERT = re.compile(r"INSERT INTO (\w+)\s*\(([^)]*)\)\s*VALUES\s*\(")


def _insert_times(match):
    table, columns = match[1], match[2]
    names = {c.strip() for c in columns.split(",")}
    missing = [c for c in INSERT_TIMES.get(table, ()) if c not in names]
    if not missing:
        return match[0]
    return f"INSERT INTO {table}({','.join(missing)},{columns}) VALUES({'now(),' * len(missing)}"


RULES = (
    (re.compile(r"skirnir_sessions\."), ""),
    (re.compile(r"\$(\d+)"), r"?\1"),
    (re.compile(r"::\w+(\[\])?"), ""),
    (re.compile(r"\s+FOR UPDATE(\s+SKIP LOCKED)?"), ""),
    (re.compile(r"(\?\d+)\*interval '(\d+) (second|day)s?'"), lambda m: f"CAST({m[1]}*{int(m[2]) * MICROS[m[3]]} AS INTEGER)"),
    (re.compile(r"interval '(\d+) (second|day)s?'"), lambda m: str(int(m[1]) * MICROS[m[2]])),
    (re.compile(r"EXTRACT\(EPOCH FROM clock_timestamp\(\)-(\w+)\)"), r"((clock_timestamp()-\1)/1000000.0)"),
    (re.compile(r"\bstrpos\("), "instr("),
    (re.compile(r"([\w'*.]+)\s*=\s*ANY\((\?\d+)\)"), r"\1 IN (SELECT value FROM json_each(\2))"),
    (re.compile(r"SELECT \* FROM unnest\((\?\d+),(\?\d+)\)"),
     r"SELECT a.value,b.value FROM json_each(\1) a JOIN json_each(\2) b ON a.key=b.key"),
    (re.compile(r"(\w+)->>'(\w+)'"), r"json_extract(\1,'$.\2')"),
    (re.compile(r"\bjsonb_build_object\("), "json_object("),
    (re.compile(r"(\w+) \|\| jsonb_build_array\((\?\d+)\)"), r"json_insert(\1,'$[#]',\2)"),
    (INSERT, _insert_times),
)


@functools.lru_cache(maxsize=512)
def translate(sql):
    """The SQLite form of a PostgreSQL statement of the session store, or None for a statement that has nothing to do
    here (a transaction-scoped advisory lock: BEGIN IMMEDIATE already holds the file's only write lock). The statements
    are fixed server code; parameters are never part of the text."""
    if ADVISORY_LOCK.fullmatch(sql):
        return None
    for pattern, replacement in RULES:
        sql = pattern.sub(replacement, sql)
    return sql


def micros(value):
    """Microseconds since the epoch of a datetime; a naive one counts as UTC (asyncpg's convention)."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return (aware - EPOCH) // timedelta(microseconds=1)


def parameter(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return micros(value)
    if isinstance(value, (list, tuple)):
        return encode([str(v) if isinstance(v, UUID) else v for v in value])
    if isinstance(value, dict):
        return encode(value)
    return value


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:  # exception-ok: whether JSON can encode a value is only known by encoding it.
        # asyncpg reports the same case as a driver error (DataError), so the `database` boundary answers UNAVAILABLE here too.
        raise sqlite3.InterfaceError("parameter cannot be encoded as JSON") from exc  # exception-ok: driver fault (see above)


def column(name, value):
    """A stored value as asyncpg returns it for the PostgreSQL column of that name."""
    if value is None:
        return None
    if name in JSON_COLUMNS:
        return json.loads(value)
    if name in TIME_COLUMNS:
        return EPOCH + timedelta(microseconds=int(value))
    if name in UUID_COLUMNS:
        return UUID(value)
    if name in BOOL_COLUMNS:
        return bool(value)
    return value


def lower(value):
    """Unicode lower case like PostgreSQL's lower(); SQLite's own only folds ASCII."""
    return value.lower() if isinstance(value, str) else value


def create_private(path):
    """Create the database file owner-only, or tighten an existing one and its WAL files to 0600 (ADR-0056). Refuses a
    symbolic link in place of the file (O_NOFOLLOW where the platform has it)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if hasattr(os, "fchmod"):   # POSIX; Windows has no owner/other mode bits to tighten
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    tighten_companions(path)


def tighten_companions(path):
    """Tighten the WAL files that exist to 0600; a symbolic link in their place is refused like one for the file."""
    if not hasattr(os, "fchmod"):   # POSIX only, like create_private
        return
    for suffix in ("-wal", "-shm"):
        try:
            fd = os.open(path + suffix, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:  # exception-ok: SQLite creates and removes these files itself, also from another
            # connection at this very moment, so whether one exists is only known by opening it (a check would race).
            continue
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


class Connection:
    """The asyncpg connection methods the store's statements use, executed in the store's worker thread."""

    def __init__(self, store):
        self.store, self.cursors = store, []

    async def execute(self, sql, *args):
        return await self.store.call(self.store.run, self, sql, args, "execute")

    async def fetch(self, sql, *args):
        return await self.store.call(self.store.run, self, sql, args, "fetch")

    async def fetchrow(self, sql, *args):
        return await self.store.call(self.store.run, self, sql, args, "fetchrow")

    async def fetchval(self, sql, *args):
        return await self.store.call(self.store.run, self, sql, args, "fetchval")

    def transaction(self):
        """A nested transaction inside the store's block, like asyncpg's within a transaction: a savepoint."""
        return Savepoint(self)

    async def cursor(self, sql, *args):
        """Rows in batches, like asyncpg's cursor; the statement closes when the transaction ends."""
        cursor = await self.store.call(self.store.run, self, sql, args, "cursor")
        while True:
            names, rows = await self.store.call(self.store.batch, cursor)
            if not rows:
                return
            for row in rows:
                yield {name: column(name, value) for name, value in zip(names, row, strict=True)}


class Savepoint:
    def __init__(self, conn):
        self.conn = conn

    async def start(self):
        await self.conn.execute("SAVEPOINT nested")

    async def commit(self):
        await self.conn.execute("RELEASE nested")

    async def rollback(self):
        await self.conn.execute("ROLLBACK TO nested")
        await self.conn.execute("RELEASE nested")


class SqliteSessionStore(CoreStore, ManagedStore, ReliabilityStore, ExchangeStore):
    driver_errors = (sqlite3.Error, OSError, TimeoutError)

    def __init__(self, path, timeout_s):
        self.path, self.timeout_s = path, timeout_s
        self.db = None
        self.closed = False    # no new calls (close() began)
        self.stopped = False   # worker thread shut down
        self.last_clock = 0
        self.transaction_time = 0
        self.lock = asyncio.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skirnir-sqlite")

    @classmethod
    async def open(cls, path, cfg):
        """Create or open the file owner-only, switch it to WAL and migrate. Returns the store or a Problem."""
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            return unavailable("session store needs SQLite 3.37 or newer in this Python")
        if not os.path.isdir(os.path.dirname(path)):
            return unavailable("session store folder for the SQLite file does not exist")
        store = cls(path, cfg.timeout_s)
        problem = None
        try:
            async with asyncio.timeout(cfg.timeout_s):
                problem = await store.call(store.connect)
        except (sqlite3.Error, OSError, TimeoutError) as exc:  # exception-ok: startup faults of the file (permissions, a
            # symbolic link, another process holding the lock, a damaged file) cannot be checked beforehand without a race.
            # Class, errno and SQLite's error name carry no content and tell the operator where to look.
            log.warning("session database startup failed: %s errno=%s sqlite=%s", type(exc).__name__,
                        getattr(exc, "errno", None), getattr(exc, "sqlite_errorname", None))
            problem = unavailable("session database startup failed")
        if failed(problem):
            await store.close()
            return problem
        return store

    # --- worker thread side -------------------------------------------------------------------------------------------

    def clock(self):
        """Microseconds since the epoch, strictly increasing within this store (PostgreSQL's clock_timestamp())."""
        now = time.time_ns() // 1000
        self.last_clock = now if now > self.last_clock else self.last_clock + 1
        return self.last_clock

    def connect(self):
        create_private(self.path)
        # The busy timeout (connect's timeout, then the pragma first of all) covers every lock wait SQLite hands to the
        # busy handler: BEGIN IMMEDIATE, the migration, every store call.
        db = sqlite3.connect(self.path, timeout=self.timeout_s, isolation_level=None)
        self.db = db
        db.execute(f"PRAGMA busy_timeout={int(self.timeout_s * 1000)}")
        db.create_function("clock_timestamp", 0, self.clock)
        db.create_function("now", 0, lambda: self.transaction_time)
        db.create_function("lower", 1, lower, deterministic=True)
        # trusted_schema off: nothing stored in the file (a trigger, a view) may call an application function.
        # secure_delete: deleted sessions and payloads are overwritten in the file, not left in free pages (ADR-0076).
        for pragma in ("PRAGMA trusted_schema=OFF", "PRAGMA foreign_keys=ON", "PRAGMA secure_delete=ON"):
            db.execute(pragma)
        if self.switch_to_wal(db) != "wal":
            return unavailable("session store file system does not support SQLite's WAL mode; use a local disk")
        db.execute("PRAGMA synchronous=FULL")   # a commit the store reported is on disk, as with PostgreSQL's default
        tighten_companions(self.path)
        db.execute("BEGIN IMMEDIATE")
        try:
            problem = sqlite_migrations.migrate(db)
        except BaseException:  # exception-ok: a failing migration statement; undo the whole migration, then report it
            db.execute("ROLLBACK")
            raise  # exception-ok: re-raised unchanged after the rollback
        db.execute("COMMIT" if problem is None else "ROLLBACK")
        return problem

    def switch_to_wal(self, db):
        """The journal mode after asking for WAL. Switching a new file needs its exclusive lock, and SQLite does not wait
        for a concurrent opener (a second process starting at the same moment) through the busy handler: it answers
        SQLITE_BUSY at once, or keeps the old mode and reports that. So this asks again until the answer is "wal" or
        timeout_s has passed; then a file system without WAL shows as the old mode, a lasting lock as the fault."""
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            except sqlite3.OperationalError as exc:  # exception-ok: whether the lock is free is only known by trying
                # (see docstring); the case is told apart by SQLite's error code, never by the message.
                if exc.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                    raise  # exception-ok: another fault, or still busy after timeout_s: the startup boundary reports it
                mode = None
            if mode == "wal" or (mode is not None and time.monotonic() >= deadline):
                return mode
            time.sleep(0.005 + random.random() * 0.02)   # spread the retries of several openers

    def begin(self):
        self.db.execute("BEGIN IMMEDIATE")
        self.transaction_time = self.clock()   # PostgreSQL's now(): the start of the transaction

    def end(self, conn, commit):
        """Close the block's cursors and commit or roll back. Runs after every statement queued before it."""
        for cursor in conn.cursors:
            cursor.close()
        conn.cursors.clear()
        if self.db is None or not self.db.in_transaction:
            return
        if not commit:
            self.db.execute("ROLLBACK")
            return
        try:
            self.db.execute("COMMIT")
        except sqlite3.Error:  # exception-ok: a failed COMMIT (disk full, I/O error) can leave the transaction open
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise  # exception-ok: re-raised unchanged for the database boundary

    def run(self, conn, sql, args, how):
        statement = translate(sql)
        if statement is None:
            return None if how != "execute" else "SELECT 0"
        cursor = self.db.execute(statement, [parameter(a) for a in args])
        if how == "cursor":
            conn.cursors.append(cursor)
            return cursor
        rows = cursor.fetchall()   # also completes a RETURNING statement and makes rowcount final
        if how == "execute":
            verb = statement.lstrip().split(None, 1)[0].upper()
            return f"{verb} {cursor.rowcount if cursor.rowcount >= 0 else len(rows)}"
        names = [d[0] for d in cursor.description or ()]
        converted = [{name: column(name, value) for name, value in zip(names, row, strict=True)} for row in rows]
        if how == "fetch":
            return converted
        if how == "fetchrow":
            return converted[0] if converted else None
        return next(iter(converted[0].values())) if converted else None

    @staticmethod
    def batch(cursor):
        return [d[0] for d in cursor.description or ()], cursor.fetchmany(64)

    def shutdown(self):
        if self.db is not None:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.close()
            self.db = None

    # --- event loop side ----------------------------------------------------------------------------------------------

    async def call(self, function, *args):
        if self.stopped:
            # The fault the driver reports for a closed connection; the `database` boundary answers UNAVAILABLE, as
            # asyncpg's closed pool does for PostgreSQL.
            raise sqlite3.ProgrammingError("session store is closed")  # exception-ok: closed store (see comment)
        return await asyncio.get_running_loop().run_in_executor(self.executor, function, *args)

    @asynccontextmanager
    async def transaction(self):
        """The store's only connection inside BEGIN IMMEDIATE. Commits unless the method returned `tx.reject(problem)`."""
        if self.closed:
            raise sqlite3.ProgrammingError("session store is closed")  # exception-ok: closed store (see call())
        async with asyncio.timeout(self.timeout_s):
            async with self.lock:
                conn = Connection(self)
                try:
                    await self.call(self.begin)
                    tx = Transaction(conn)
                    yield conn, tx
                except BaseException:  # exception-ok: driver fault, cancellation or timeout mid-block
                    # Queue the rollback; the worker thread runs it after the statement it may still be executing and
                    # before the BEGIN of whoever takes the lock next.
                    if not self.stopped:
                        self.executor.submit(self.end, conn, False)
                    raise  # exception-ok: re-raised unchanged after queueing the rollback
                await self.call(self.end, conn, tx.problem is None)

    @asynccontextmanager
    async def connection(self):
        async with self.transaction() as (conn, _tx):
            yield conn

    @database
    async def health(self):
        async with self.connection() as conn:
            return await conn.fetchval("SELECT 1") == 1

    async def close(self):
        """Refuse new calls, wait at most timeout_s for the running one, then roll back what is open and close the file.
        asyncio.wait reports the timeouts as state, not as an exception."""
        if self.closed:
            return
        self.closed = True
        waiter = asyncio.ensure_future(self.lock.acquire())
        held, _ = await asyncio.wait({waiter}, timeout=self.timeout_s)
        if not held:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        closing = asyncio.ensure_future(self.call(self.shutdown))
        done, _ = await asyncio.wait({closing}, timeout=self.timeout_s)
        if done and closing.exception() is not None:
            log.warning("session database close: %s", type(closing.exception()).__name__)
        self.stopped = True
        self.executor.shutdown(wait=False)
        if held:
            self.lock.release()
