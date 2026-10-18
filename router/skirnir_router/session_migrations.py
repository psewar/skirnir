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
""", """
ALTER TABLE skirnir_sessions.sessions DROP CONSTRAINT sessions_mode_check;
ALTER TABLE skirnir_sessions.sessions ADD CONSTRAINT sessions_mode_check CHECK (mode IN ('client-managed', 'skirnir-managed'));
CREATE TABLE skirnir_sessions.turns (
    owner text NOT NULL,
    session_id uuid NOT NULL,
    event_id text NOT NULL,
    fingerprint text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'completed', 'failed')),
    lease uuid NOT NULL,
    lease_until timestamptz NOT NULL,
    input_end_seq bigint NOT NULL,
    input_start_seq bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, session_id, event_id),
    FOREIGN KEY (owner, session_id) REFERENCES skirnir_sessions.sessions(owner, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX one_pending_turn ON skirnir_sessions.turns(owner, session_id) WHERE state='pending';
CREATE TABLE skirnir_sessions.compactions (
    owner text NOT NULL,
    session_id uuid NOT NULL,
    version bigint NOT NULL CHECK (version>0),
    through_seq bigint NOT NULL CHECK (through_seq>=0),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, session_id, version),
    FOREIGN KEY (owner, session_id) REFERENCES skirnir_sessions.sessions(owner, id) ON DELETE CASCADE
);
""", """
ALTER TABLE skirnir_sessions.sessions ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX sessions_discovery ON skirnir_sessions.sessions(owner,updated_at DESC,id DESC);
CREATE INDEX sessions_project_status ON skirnir_sessions.sessions(owner,project,status,updated_at DESC,id DESC);
CREATE FUNCTION skirnir_sessions.touch_session() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE skirnir_sessions.sessions SET updated_at=clock_timestamp() WHERE owner=NEW.owner AND id=NEW.session_id;
    RETURN NEW;
END $$;
CREATE TRIGGER events_touch AFTER INSERT ON skirnir_sessions.events FOR EACH ROW EXECUTE FUNCTION skirnir_sessions.touch_session();
CREATE TRIGGER compactions_touch AFTER INSERT ON skirnir_sessions.compactions FOR EACH ROW EXECUTE FUNCTION skirnir_sessions.touch_session();
CREATE TABLE skirnir_sessions.handoff_imports (
    owner text NOT NULL, event_id text NOT NULL, fingerprint text NOT NULL, session_id uuid NOT NULL,
    PRIMARY KEY(owner,event_id),
    FOREIGN KEY(owner,session_id) REFERENCES skirnir_sessions.sessions(owner,id) ON DELETE CASCADE
);
CREATE TABLE skirnir_sessions.session_memory_scopes (
    owner text NOT NULL, session_id uuid NOT NULL, project text NOT NULL, payload jsonb NOT NULL,
    PRIMARY KEY(owner,session_id,project),
    FOREIGN KEY(owner,session_id) REFERENCES skirnir_sessions.sessions(owner,id) ON DELETE CASCADE
);
CREATE TABLE skirnir_sessions.memory_outbox (
    operation_id uuid PRIMARY KEY,
    owner text NOT NULL,
    project text NOT NULL,
    scope jsonb NOT NULL,
    fingerprint text NOT NULL,
    delete_requested boolean NOT NULL DEFAULT false,
    payload jsonb,
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','accepted','dead','revoked','cancelled','delete_pending','deleted')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease uuid,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_pending ON skirnir_sessions.memory_outbox(available_at,created_at) WHERE state IN ('pending','processing','delete_pending');
CREATE INDEX outbox_owner ON skirnir_sessions.memory_outbox(owner,operation_id);
""", """
ALTER TABLE skirnir_sessions.memory_outbox ADD COLUMN last_error text;
ALTER TABLE skirnir_sessions.memory_outbox ADD COLUMN retry_ids jsonb NOT NULL DEFAULT '[]';
ALTER TABLE skirnir_sessions.memory_outbox ADD COLUMN retry_base integer NOT NULL DEFAULT 0;
ALTER TABLE skirnir_sessions.memory_outbox ADD COLUMN retry_rounds integer NOT NULL DEFAULT 0;
CREATE INDEX outbox_owner_active ON skirnir_sessions.memory_outbox(owner)
    WHERE state IN ('pending','processing','delete_pending');
CREATE INDEX outbox_active_created ON skirnir_sessions.memory_outbox(created_at)
    WHERE state IN ('pending','processing','delete_pending');
CREATE INDEX outbox_payload_expiry ON skirnir_sessions.memory_outbox(updated_at)
    WHERE payload IS NOT NULL AND state IN ('accepted','dead','revoked','cancelled','deleted');
CREATE TABLE skirnir_sessions.outbox_totals(state text PRIMARY KEY, jobs bigint NOT NULL CHECK(jobs>=0));
INSERT INTO skirnir_sessions.outbox_totals SELECT state,count(*) FROM skirnir_sessions.memory_outbox GROUP BY state;
CREATE FUNCTION skirnir_sessions.count_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND OLD.state=NEW.state THEN RETURN NULL; END IF;
    PERFORM pg_advisory_xact_lock(7365471202493);
    IF TG_OP IN ('DELETE','UPDATE') THEN
        UPDATE skirnir_sessions.outbox_totals SET jobs=jobs-1 WHERE state=OLD.state;
    END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        INSERT INTO skirnir_sessions.outbox_totals VALUES(NEW.state,1)
        ON CONFLICT(state) DO UPDATE SET jobs=skirnir_sessions.outbox_totals.jobs+1;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER outbox_totals AFTER INSERT OR DELETE OR UPDATE OF state ON skirnir_sessions.memory_outbox
    FOR EACH ROW EXECUTE FUNCTION skirnir_sessions.count_outbox();
""")


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
