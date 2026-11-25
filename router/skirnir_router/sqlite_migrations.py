"""Ordered migrations of the SQLite session store (ADR-0114, ADR-0122). Each migration is a tuple of statements.

Migration 1 is the schema PostgreSQL reaches after its migrations 1-6 (session_migrations.py), in SQLite types: UUIDs as
canonical lower-case text, JSON as text checked by json_valid, timestamps as integer microseconds since the Unix epoch
(UTC), booleans as 0/1. STRICT tables reject a value of the wrong type. Triggers use no application function, so the
connection can keep `trusted_schema` off. The statements name tables without the `skirnir_sessions.` schema: the file
holds nothing else. All DDL is fixed server code, never client input.
Do not edit released migrations: append a new version instead, here and in session_migrations.py.
"""
from .outcome import unavailable

OUTBOX_STATES = ('pending', 'processing', 'accepted', 'completed', 'dead', 'revoked', 'cancelled', 'delete_pending', 'deleted')
# Added by migration 2 (ADR-0145); kept apart so migration 1 stays exactly as released.
REVIEW_STATES = ('awaiting_review', 'rejected')

MIGRATIONS = ((
    """CREATE TABLE sessions (
        owner TEXT NOT NULL,
        id TEXT NOT NULL,
        project TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('client-managed', 'skirnir-managed')),
        data_class TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
        metadata TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata)),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (owner, id)
    ) STRICT""",
    "CREATE INDEX sessions_discovery ON sessions(owner, updated_at DESC, id DESC)",
    "CREATE INDEX sessions_project_status ON sessions(owner, project, status, updated_at DESC, id DESC)",
    """CREATE TABLE events (
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL CHECK (seq > 0),
        event_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('message', 'checkpoint')),
        version INTEGER,
        payload TEXT NOT NULL CHECK (json_valid(payload)),
        created_at INTEGER NOT NULL,
        CHECK ((kind = 'message' AND version IS NULL) OR (kind = 'checkpoint' AND version > 0 AND version IS NOT NULL)),
        PRIMARY KEY (owner, session_id, seq),
        UNIQUE (owner, session_id, event_id),
        FOREIGN KEY (owner, session_id) REFERENCES sessions(owner, id) ON DELETE CASCADE
    ) STRICT""",
    "CREATE UNIQUE INDEX checkpoint_versions ON events(owner, session_id, version) WHERE kind = 'checkpoint'",
    """CREATE TABLE turns (
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'failed')),
        lease TEXT NOT NULL,
        lease_until INTEGER NOT NULL,
        input_end_seq INTEGER NOT NULL,
        input_start_seq INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (owner, session_id, event_id),
        FOREIGN KEY (owner, session_id) REFERENCES sessions(owner, id) ON DELETE CASCADE
    ) STRICT""",
    "CREATE UNIQUE INDEX one_pending_turn ON turns(owner, session_id) WHERE state = 'pending'",
    """CREATE TABLE compactions (
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        through_seq INTEGER NOT NULL CHECK (through_seq >= 0),
        payload TEXT NOT NULL CHECK (json_valid(payload)),
        created_at INTEGER NOT NULL,
        PRIMARY KEY (owner, session_id, version),
        FOREIGN KEY (owner, session_id) REFERENCES sessions(owner, id) ON DELETE CASCADE
    ) STRICT""",
    # PostgreSQL's touch_session() sets updated_at to clock_timestamp(); here the inserted row's created_at, the start of
    # the writing transaction on the store clock (one writer at a time, so later writes get later times).
    """CREATE TRIGGER events_touch AFTER INSERT ON events BEGIN
        UPDATE sessions SET updated_at = NEW.created_at WHERE owner = NEW.owner AND id = NEW.session_id;
    END""",
    """CREATE TRIGGER compactions_touch AFTER INSERT ON compactions BEGIN
        UPDATE sessions SET updated_at = NEW.created_at WHERE owner = NEW.owner AND id = NEW.session_id;
    END""",
    """CREATE TABLE handoff_imports (
        owner TEXT NOT NULL, event_id TEXT NOT NULL, fingerprint TEXT NOT NULL, session_id TEXT NOT NULL,
        PRIMARY KEY (owner, event_id),
        FOREIGN KEY (owner, session_id) REFERENCES sessions(owner, id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE session_memory_scopes (
        owner TEXT NOT NULL, session_id TEXT NOT NULL, project TEXT NOT NULL, payload TEXT NOT NULL CHECK (json_valid(payload)),
        PRIMARY KEY (owner, session_id, project),
        FOREIGN KEY (owner, session_id) REFERENCES sessions(owner, id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE memory_outbox (
        operation_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        project TEXT NOT NULL,
        scope TEXT NOT NULL CHECK (json_valid(scope)),
        fingerprint TEXT NOT NULL,
        delete_requested INTEGER NOT NULL DEFAULT 0 CHECK (delete_requested IN (0, 1)),
        payload TEXT CHECK (payload IS NULL OR json_valid(payload)),
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'processing', 'accepted', 'completed', 'dead',
                                                             'revoked', 'cancelled', 'delete_pending', 'deleted')),
        attempts INTEGER NOT NULL DEFAULT 0,
        available_at INTEGER NOT NULL,
        lease TEXT,
        lease_until INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        last_error TEXT,
        retry_ids TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(retry_ids)),
        retry_base INTEGER NOT NULL DEFAULT 0,
        retry_rounds INTEGER NOT NULL DEFAULT 0,
        upstream_attempts INTEGER NOT NULL DEFAULT 0,
        upstream_base INTEGER NOT NULL DEFAULT 0,
        verify_checks INTEGER NOT NULL DEFAULT 0,
        accepted_at INTEGER,
        session_id TEXT
    ) STRICT""",
    "CREATE INDEX outbox_pending ON memory_outbox(available_at, created_at) WHERE state IN ('pending', 'processing', 'delete_pending')",
    "CREATE INDEX outbox_owner ON memory_outbox(owner, operation_id)",
    "CREATE INDEX outbox_owner_active ON memory_outbox(owner) WHERE state IN ('pending', 'processing', 'delete_pending')",
    "CREATE INDEX outbox_active_created ON memory_outbox(created_at) WHERE state IN ('pending', 'processing', 'delete_pending')",
    """CREATE INDEX outbox_payload_expiry ON memory_outbox(updated_at)
        WHERE payload IS NOT NULL AND state IN ('completed', 'dead', 'revoked', 'cancelled', 'deleted')""",
    "CREATE INDEX outbox_verify ON memory_outbox(available_at) WHERE state = 'accepted'",
    "CREATE INDEX outbox_session ON memory_outbox(owner, session_id) WHERE session_id IS NOT NULL",
    # Exact per-state counts like PostgreSQL's count_outbox(): one row per state from the start, so the triggers only
    # add and subtract (BEGIN IMMEDIATE already serializes every writer, the advisory lock has no counterpart here).
    "CREATE TABLE outbox_totals (state TEXT PRIMARY KEY, jobs INTEGER NOT NULL CHECK (jobs >= 0)) STRICT",
    "INSERT INTO outbox_totals VALUES " + ",".join(f"('{state}', 0)" for state in OUTBOX_STATES),
    """CREATE TRIGGER outbox_totals_insert AFTER INSERT ON memory_outbox BEGIN
        UPDATE outbox_totals SET jobs = jobs + 1 WHERE state = NEW.state;
    END""",
    """CREATE TRIGGER outbox_totals_delete AFTER DELETE ON memory_outbox BEGIN
        UPDATE outbox_totals SET jobs = jobs - 1 WHERE state = OLD.state;
    END""",
    """CREATE TRIGGER outbox_totals_update AFTER UPDATE OF state ON memory_outbox WHEN OLD.state <> NEW.state BEGIN
        UPDATE outbox_totals SET jobs = jobs - 1 WHERE state = OLD.state;
        UPDATE outbox_totals SET jobs = jobs + 1 WHERE state = NEW.state;
    END""",
), (
    # PostgreSQL migration 7 (ADR-0145): the outbox states awaiting_review and rejected. SQLite cannot change a CHECK
    # constraint, so the table is rebuilt the documented way (new table, copy, drop, rename, then its indexes and
    # triggers again); the rows and their counts in outbox_totals stay as they are.
    """CREATE TABLE memory_outbox_next (
        operation_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        project TEXT NOT NULL,
        scope TEXT NOT NULL CHECK (json_valid(scope)),
        fingerprint TEXT NOT NULL,
        delete_requested INTEGER NOT NULL DEFAULT 0 CHECK (delete_requested IN (0, 1)),
        payload TEXT CHECK (payload IS NULL OR json_valid(payload)),
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'processing', 'accepted', 'completed', 'dead',
                                                             'revoked', 'cancelled', 'delete_pending', 'deleted',
                                                             'awaiting_review', 'rejected')),
        attempts INTEGER NOT NULL DEFAULT 0,
        available_at INTEGER NOT NULL,
        lease TEXT,
        lease_until INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        last_error TEXT,
        retry_ids TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(retry_ids)),
        retry_base INTEGER NOT NULL DEFAULT 0,
        retry_rounds INTEGER NOT NULL DEFAULT 0,
        upstream_attempts INTEGER NOT NULL DEFAULT 0,
        upstream_base INTEGER NOT NULL DEFAULT 0,
        verify_checks INTEGER NOT NULL DEFAULT 0,
        accepted_at INTEGER,
        session_id TEXT
    ) STRICT""",
    "INSERT INTO memory_outbox_next SELECT * FROM memory_outbox",
    "DROP TABLE memory_outbox",
    "ALTER TABLE memory_outbox_next RENAME TO memory_outbox",
    "CREATE INDEX outbox_pending ON memory_outbox(available_at, created_at) WHERE state IN ('pending', 'processing', 'delete_pending')",
    "CREATE INDEX outbox_owner ON memory_outbox(owner, operation_id)",
    "CREATE INDEX outbox_owner_active ON memory_outbox(owner) WHERE state IN ('pending', 'processing', 'delete_pending')",
    "CREATE INDEX outbox_active_created ON memory_outbox(created_at) WHERE state IN ('pending', 'processing', 'delete_pending')",
    """CREATE INDEX outbox_payload_expiry ON memory_outbox(updated_at)
        WHERE payload IS NOT NULL AND state IN ('completed', 'dead', 'revoked', 'cancelled', 'deleted')""",
    "CREATE INDEX outbox_verify ON memory_outbox(available_at) WHERE state = 'accepted'",
    "CREATE INDEX outbox_session ON memory_outbox(owner, session_id) WHERE session_id IS NOT NULL",
    "CREATE INDEX outbox_review ON memory_outbox(available_at) WHERE state = 'awaiting_review'",
    "INSERT INTO outbox_totals VALUES " + ",".join(f"('{state}', 0)" for state in REVIEW_STATES),
    """CREATE TRIGGER outbox_totals_insert AFTER INSERT ON memory_outbox BEGIN
        UPDATE outbox_totals SET jobs = jobs + 1 WHERE state = NEW.state;
    END""",
    """CREATE TRIGGER outbox_totals_delete AFTER DELETE ON memory_outbox BEGIN
        UPDATE outbox_totals SET jobs = jobs - 1 WHERE state = OLD.state;
    END""",
    """CREATE TRIGGER outbox_totals_update AFTER UPDATE OF state ON memory_outbox WHEN OLD.state <> NEW.state BEGIN
        UPDATE outbox_totals SET jobs = jobs - 1 WHERE state = OLD.state;
        UPDATE outbox_totals SET jobs = jobs + 1 WHERE state = NEW.state;
    END""",
),)


def migrate(conn):
    """Apply pending migrations on a sqlite3 connection inside the caller's BEGIN IMMEDIATE transaction, which also
    serializes a second process opening the same file. Returns None, or a Problem when the file's schema is newer than
    this router (nothing changed then)."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY) STRICT")
    version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    if version > len(MIGRATIONS):
        return unavailable("session database schema is newer than this router")
    for n, statements in enumerate(MIGRATIONS, 1):
        if n > version:
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version VALUES (?)", (n,))
    return None
