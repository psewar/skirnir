"""Ordered, transactional migrations. Python packaging also works with deploy.py.

Dedicated schema; no pgvector. All DDL is fixed server code, never client input.
Do not edit released migrations: append a new version instead.
"""

MIGRATIONS = ("""
CREATE TABLE skirnir_sessions.sessions (
    owner text NOT NULL,
    id uuid NOT NULL,
    project text NOT NULL,
    mode text NOT NULL CHECK (mode = 'client-managed'),
    data_class text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, id)
);
CREATE TABLE skirnir_sessions.events (
    owner text NOT NULL,
    session_id uuid NOT NULL,
    seq bigint NOT NULL CHECK (seq > 0),
    event_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('message', 'checkpoint')),
    version bigint,
    CHECK ((kind = 'message' AND version IS NULL) OR (kind = 'checkpoint' AND version > 0 AND version IS NOT NULL)),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, session_id, seq),
    UNIQUE (owner, session_id, event_id),
    FOREIGN KEY (owner, session_id) REFERENCES skirnir_sessions.sessions(owner, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX checkpoint_versions ON skirnir_sessions.events(owner, session_id, version) WHERE kind = 'checkpoint';
""",)


async def migrate(conn):
    async with conn.transaction():
        # Serialize concurrent router starts, including creation of the version table.
        await conn.execute("SELECT pg_advisory_xact_lock(7365471202491)")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS skirnir_sessions")
        await conn.execute("CREATE TABLE IF NOT EXISTS skirnir_sessions.schema_version (version integer PRIMARY KEY)")
        version = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM skirnir_sessions.schema_version")
        if version > len(MIGRATIONS):
            raise RuntimeError("session database schema is newer than this router")
        for n, sql in enumerate(MIGRATIONS, 1):
            if n > version:
                await conn.execute(sql)
                await conn.execute("INSERT INTO skirnir_sessions.schema_version VALUES ($1)", n)
