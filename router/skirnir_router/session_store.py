"""Persistence boundary for explicit client events; never called by inference.

Owner is supplied by authenticated server code. No method performs an unscoped
session lookup. Messages and checkpoint versions are immutable until deletion.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Protocol
from uuid import UUID, uuid4

from .session_migrations import migrate


class SessionNotFound(Exception):
    pass


class SessionConflict(Exception):
    pass


class SessionUnavailable(Exception):
    pass


class SessionStore(Protocol):
    async def create(self, owner: str, project: str, data_class: str, metadata: dict) -> dict: ...
    async def get(self, owner: str, session_id: UUID) -> dict: ...
    async def update(self, owner: str, session_id: UUID, status: str | None, metadata: dict | None) -> dict: ...
    async def delete(self, owner: str, session_id: UUID) -> None: ...
    async def append(self, owner: str, session_id: UUID, event_id: str, kind: str, payload: dict) -> tuple[dict, bool]: ...
    async def read(self, owner: str, session_id: UUID, kind: str, after: int, limit: int) -> list[dict]: ...
    async def close(self) -> None: ...


def record(row):
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
    return result


class PostgresSessionStore:
    def __init__(self, pool, timeout_s):
        self.pool, self.timeout_s = pool, timeout_s

    @classmethod
    async def open(cls, dsn, cfg):
        # Optional dependency: disabled installations do not need asyncpg.
        import asyncpg

        pool = None
        try:
            async with asyncio.timeout(cfg.timeout_s):
                async def setup(conn):
                    await conn.set_type_codec("jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads, format="text")
                pool = await asyncpg.create_pool(dsn, min_size=cfg.min_pool_size, max_size=cfg.max_pool_size,
                                                timeout=cfg.timeout_s, command_timeout=cfg.timeout_s, init=setup)
                async with pool.acquire() as conn:
                    await migrate(conn)
        except Exception:  # noqa: BLE001 -- driver errors may contain DSNs/secrets; never expose them
            if pool is not None:
                pool.terminate()
            raise SessionUnavailable("session database startup failed") from None
        return cls(pool, cfg.timeout_s)

    @asynccontextmanager
    async def connection(self):
        try:
            async with asyncio.timeout(self.timeout_s):
                async with self.pool.acquire() as conn:
                    yield conn
        except (SessionNotFound, SessionConflict):
            raise
        except Exception:  # noqa: BLE001 -- redact database/connection exceptions
            raise SessionUnavailable("session database unavailable") from None

    async def close(self):
        try:
            async with asyncio.timeout(self.timeout_s):
                await self.pool.close()
        except TimeoutError:
            self.pool.terminate()

    async def create(self, owner, project, data_class, metadata):
        async with self.connection() as conn:
            row = await conn.fetchrow("""INSERT INTO skirnir_sessions.sessions (owner,id,project,mode,data_class,metadata)
                VALUES ($1,$2,$3,'client-managed',$4,$5) RETURNING *""", owner, uuid4(), project, data_class, metadata)
            return record(row)

    async def _get(self, conn, owner, session_id, lock=False):
        sql = "SELECT * FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2"
        row = await conn.fetchrow(sql + (" FOR UPDATE" if lock else ""), owner, session_id)
        if row is None:
            raise SessionNotFound
        return record(row)

    async def get(self, owner, session_id):
        async with self.connection() as conn:
            return await self._get(conn, owner, session_id)

    async def update(self, owner, session_id, status, metadata):
        async with self.connection() as conn, conn.transaction():
            await self._get(conn, owner, session_id, lock=True)
            row = await conn.fetchrow("""UPDATE skirnir_sessions.sessions SET status=COALESCE($3,status),
                metadata=COALESCE($4,metadata) WHERE owner=$1 AND id=$2 RETURNING *""", owner, session_id, status, metadata)
            return record(row)

    async def delete(self, owner, session_id):
        async with self.connection() as conn:
            result = await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2", owner, session_id)
            if result == "DELETE 0":
                raise SessionNotFound

    async def append(self, owner, session_id, event_id, kind, payload):
        if kind not in ("message", "checkpoint"):
            raise ValueError("unsupported event kind")
        async with self.connection() as conn, conn.transaction():
            session = await self._get(conn, owner, session_id, lock=True)
            # Lock serializes writers/deletion, sequence allocation and idempotency.
            old = await conn.fetchrow("SELECT * FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2 AND event_id=$3",
                                      owner, session_id, event_id)
            if old:
                if old["kind"] != kind or old["payload"] != payload:
                    raise SessionConflict("event_id already used with different content")
                return record(old), False
            if session["status"] != "active":
                raise SessionConflict("session is archived")
            seq = await conn.fetchval("SELECT COALESCE(MAX(seq),0)+1 FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2",
                                      owner, session_id)
            version = None
            if kind == "checkpoint":
                version = await conn.fetchval("""SELECT COALESCE(MAX(version),0)+1 FROM skirnir_sessions.events
                    WHERE owner=$1 AND session_id=$2 AND kind='checkpoint'""", owner, session_id)
            row = await conn.fetchrow("""INSERT INTO skirnir_sessions.events (owner,session_id,seq,event_id,kind,version,payload)
                VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *""", owner, session_id, seq, event_id, kind, version, payload)
            return record(row), True

    async def read(self, owner, session_id, kind, after, limit):
        async with self.connection() as conn, conn.transaction():
            await self._get(conn, owner, session_id)
            rows = await conn.fetch("""SELECT * FROM skirnir_sessions.events
                WHERE owner=$1 AND session_id=$2 AND kind=$3 AND seq>$4 ORDER BY seq LIMIT $5""", owner, session_id, kind, after, limit)
            return [record(row) for row in rows]
