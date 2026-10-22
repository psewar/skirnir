"""Persistence boundary for explicit events and opt-in managed inference.

Owner is supplied by authenticated server code. No method performs an unscoped
session lookup. Messages and checkpoint versions are immutable until deletion.
Every public method returns its result or a Problem (outcome.py); database faults
become UNAVAILABLE at the `database` boundary.
"""

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from typing import Protocol
from uuid import UUID, uuid4

from .outcome import conflict, failed, invalid, unavailable
from .session_migrations import migrate
from .session_types import NOT_FOUND, Transaction, database, record
from .managed_store import ManagedStore
from .reliability_store import ReliabilityStore
from .exchange_store import ExchangeStore


class SessionStore(Protocol):
    async def create(self, owner: str, project: str, data_class: str, metadata: dict, mode: str = "client-managed") -> dict: ...
    async def get(self, owner: str, session_id: UUID) -> dict: ...
    async def update(self, owner: str, session_id: UUID, status: str | None, metadata: dict | None) -> dict: ...
    async def delete(self, owner: str, session_id: UUID) -> None: ...
    async def append(self, owner: str, session_id: UUID, event_id: str, kind: str, payload: dict) -> tuple[dict, bool]: ...
    async def read(self, owner: str, session_id: UUID, kind: str, after: int, limit: int) -> list[dict]: ...
    async def claim(self, owner, session_id, event_id, fingerprint, messages, cfg) -> dict: ...
    async def renew(self, owner, session_id, lease, seconds) -> None: ...
    async def finish_turn(self, owner, session_id, lease, message, checkpoint=None) -> None: ...
    async def fail_turn(self, owner, session_id, lease) -> None: ...
    async def context(self, owner, session_id, cfg) -> dict: ...
    async def compact(self, owner, session_id, lease, through_seq, payload) -> dict: ...
    async def compactions(self, owner, session_id, after, limit) -> list[dict]: ...
    async def search(self, owner, session_id, query, after, limit) -> list[dict]: ...
    async def outbox_put(self, operation, scope, payload, fingerprint, capacity, owner_capacity=0) -> dict: ...
    async def outbox_claim(self, lease_s, max_attempts) -> dict: ...
    async def outbox_finish(self, operation, lease, status, delay=0, error=None) -> None: ...
    async def outbox_get(self, owner, operation) -> dict: ...
    async def outbox_find(self, operation) -> dict: ...
    async def outbox_cancel(self, owner, operation) -> None: ...
    async def outbox_delete(self, owner, operation, scope) -> None: ...
    async def outbox_retry(self, owner, operation, retry_id, scope, capacity, owner_capacity=0) -> dict: ...
    async def outbox_oldest(self) -> float: ...
    async def outbox_counts(self) -> dict: ...
    async def cleanup(self, session_days, archived_days, outbox_days) -> dict: ...
    async def discover(self, owner, projects, project, status, since, before_time, before_id, limit, query) -> list[dict]: ...
    async def import_session(self, owner, event, fingerprint, project, data_class, mode, metadata, messages, checkpoint) -> tuple[dict, bool]: ...
    async def remember_memory_scope(self, owner, sid, lease, scope) -> None: ...
    async def health(self) -> bool: ...
    async def close(self) -> None: ...


class PostgresSessionStore(ManagedStore, ReliabilityStore, ExchangeStore):
    # Faults that the `database` boundary turns into UNAVAILABLE; open() adds the driver's own.
    driver_errors = (OSError, TimeoutError)

    def __init__(self, pool, timeout_s):
        self.pool, self.timeout_s = pool, timeout_s

    @classmethod
    async def open(cls, dsn, cfg):
        """Connect and migrate. Returns the store or a Problem."""
        # Optional dependency: disabled installations do not need it.
        if importlib.util.find_spec("asyncpg") is None:
            return unavailable("session store driver missing; install router/requirements-session.txt")
        import asyncpg
        store = cls(None, cfg.timeout_s)
        store.driver_errors = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)
        problem = None
        try:
            async with asyncio.timeout(cfg.timeout_s):
                async def setup(conn):
                    await conn.set_type_codec("jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads, format="text")
                store.pool = await asyncpg.create_pool(dsn, min_size=cfg.min_pool_size, max_size=cfg.max_pool_size,
                                                       timeout=cfg.timeout_s, command_timeout=cfg.timeout_s, init=setup)
                async with store.pool.acquire() as conn:
                    problem = await migrate(conn)
        except Exception:  # noqa: BLE001  # exception-ok: startup faults (unreachable host, bad credentials, malformed DSN)
            # cannot be checked beforehand, and driver messages may contain the DSN or its password: report a fixed problem only.
            problem = unavailable("session database startup failed")
        if failed(problem):
            if store.pool is not None:
                store.pool.terminate()
            return problem
        return store

    @asynccontextmanager
    async def connection(self):
        async with asyncio.timeout(self.timeout_s):
            async with self.pool.acquire() as conn:
                yield conn

    @asynccontextmanager
    async def transaction(self):
        """Connection plus transaction. Commits unless the method returned `tx.reject(problem)`."""
        async with self.connection() as conn:
            native = conn.transaction()
            await native.start()
            tx = Transaction(conn)
            try:
                yield conn, tx
            except BaseException:  # exception-ok: driver fault or cancellation mid-method
                # Undo partial writes, then let the `database` boundary (or the cancelled caller) handle it.
                await native.rollback()
                raise  # exception-ok: re-raised unchanged after the rollback
            if tx.problem is None:
                await native.commit()
            else:
                await native.rollback()

    @database
    async def health(self):
        async with self.connection() as conn:
            return await conn.fetchval("SELECT 1") == 1

    async def close(self):
        closing = asyncio.ensure_future(self.pool.close())
        done, _ = await asyncio.wait({closing}, timeout=self.timeout_s)
        if not done:   # graceful close did not finish in time: drop the connections
            self.pool.terminate()
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)

    @database
    async def create(self, owner, project, data_class, metadata, mode="client-managed"):
        async with self.connection() as conn:
            row = await conn.fetchrow("""INSERT INTO skirnir_sessions.sessions (owner,id,project,mode,data_class,metadata)
                VALUES ($1,$2,$3,$6,$4,$5) RETURNING *""", owner, uuid4(), project, data_class, metadata, mode)
            return record(row)

    async def _get(self, conn, owner, session_id, lock=False):
        sql = "SELECT * FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2"
        row = await conn.fetchrow(sql + (" FOR UPDATE" if lock else ""), owner, session_id)
        return NOT_FOUND if row is None else record(row)

    @database
    async def get(self, owner, session_id):
        async with self.connection() as conn:
            return await self._get(conn, owner, session_id)

    @database
    async def update(self, owner, session_id, status, metadata):
        async with self.transaction() as (conn, tx):
            session = await self._get(conn, owner, session_id, lock=True)
            if failed(session):
                return tx.reject(session)
            busy = await self._idle(conn, owner, session_id)
            if failed(busy):
                return tx.reject(busy)
            row = await conn.fetchrow("""UPDATE skirnir_sessions.sessions SET status=COALESCE($3,status),
                metadata=COALESCE($4,metadata),updated_at=clock_timestamp() WHERE owner=$1 AND id=$2 RETURNING *""", owner, session_id, status, metadata)
            return record(row)

    @database
    async def delete(self, owner, session_id):
        async with self.connection() as conn:
            result = await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2", owner, session_id)
            return NOT_FOUND if result == "DELETE 0" else None

    @database
    async def append(self, owner, session_id, event_id, kind, payload):
        if kind not in ("message", "checkpoint"):
            return invalid("unsupported event kind")
        async with self.transaction() as (conn, tx):
            session = await self._get(conn, owner, session_id, lock=True)
            if failed(session):
                return tx.reject(session)
            if session["mode"] == "skirnir-managed" and kind == "message":
                return tx.reject(conflict("managed messages are written through inference only"))
            # Lock serializes writers/deletion, sequence allocation and idempotency.
            old = await conn.fetchrow("SELECT * FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2 AND event_id=$3",
                                      owner, session_id, event_id)
            if old:
                if old["kind"] != kind or old["payload"] != payload:
                    return tx.reject(conflict("event_id already used with different content"))
                return record(old), False
            if session["status"] != "active":
                return tx.reject(conflict("session is archived"))
            seq = await conn.fetchval("SELECT COALESCE(MAX(seq),0)+1 FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2",
                                      owner, session_id)
            version = None
            if kind == "checkpoint":
                version = await conn.fetchval("""SELECT COALESCE(MAX(version),0)+1 FROM skirnir_sessions.events
                    WHERE owner=$1 AND session_id=$2 AND kind='checkpoint'""", owner, session_id)
            row = await conn.fetchrow("""INSERT INTO skirnir_sessions.events (owner,session_id,seq,event_id,kind,version,payload)
                VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *""", owner, session_id, seq, event_id, kind, version, payload)
            return record(row), True

    @database
    async def read(self, owner, session_id, kind, after, limit):
        async with self.transaction() as (conn, tx):
            session = await self._get(conn, owner, session_id)
            if failed(session):
                return tx.reject(session)
            rows = await conn.fetch("""SELECT * FROM skirnir_sessions.events
                WHERE owner=$1 AND session_id=$2 AND kind=$3 AND seq>$4 ORDER BY seq LIMIT $5""", owner, session_id, kind, after, limit)
            return [record(row) for row in rows]
