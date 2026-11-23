"""Session store engines for the integration tests (ADR-0114).

Each contract test class runs once per engine: SQLite always (a fresh file in a temporary folder per test, no server
needed), PostgreSQL when SKIRNIR_TEST_SESSION_DSN points to a disposable database. Both open through
open_session_store, the factory the router uses. Statements the tests run directly (`store.connection()`) are written
for PostgreSQL; the SQLite store translates them like its own.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router.session_config import SessionConfig  # noqa: E402
from skirnir_router.session_store import open_session_store  # noqa: E402

DSN = os.environ.get("SKIRNIR_TEST_SESSION_DSN")
POSTGRES_ONLY = unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")


class PostgresEngine:
    name = "postgresql"

    def setup(self):
        self.url = DSN

    def teardown(self):
        pass

    async def open(self, cfg=None):
        return await open_session_store(self.url, cfg or SessionConfig(enabled=True), None)

    @asynccontextmanager
    async def hold(self, store, owner, sid):
        """Another writer holds the session's row lock until the block ends."""
        async with store.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2 FOR UPDATE", owner, sid)
            yield


class SqliteEngine:
    name = "sqlite"

    def setup(self):
        self.folder = tempfile.mkdtemp(prefix="skirnir-sessions-test-")
        self.path = os.path.join(self.folder, "sessions.db")
        self.url = "sqlite:///" + self.path   # absolute: also usable where the router resolves the state folder itself

    def teardown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    async def open(self, cfg=None):
        return await open_session_store("sqlite:///sessions.db", cfg or SessionConfig(enabled=True), self.folder)

    @asynccontextmanager
    async def hold(self, store, owner, sid):
        """Another process holds the file's write lock until the block ends (a second connection, as a backup tool or
        an accidental second router would)."""
        other = sqlite3.connect(self.path, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            yield
        finally:
            other.execute("ROLLBACK")
            other.close()


def prepare(case):
    """Give a test case `case.engine` from its class attribute `engine_class`; the engine's folder goes with the test."""
    case.engine = case.engine_class()
    case.engine.setup()
    case.addCleanup(case.engine.teardown)
    return case.engine
