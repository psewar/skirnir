"""Session discovery, atomic handoff initialization and memory-scope references.

Methods return their result or a Problem (outcome.py).
"""
import json
from uuid import uuid4

from .outcome import conflict, failed, too_large
from .session_types import database, record


class ExchangeStore:
    @database
    async def discover(self, owner, projects, project, status, since, before_time, before_id, limit, query):
        async with self.connection() as conn:
            rows = await conn.fetch("""SELECT id,project,status,mode,data_class,created_at,updated_at FROM skirnir_sessions.sessions
                WHERE owner=$1 AND ('*'=ANY($2::text[]) OR project=ANY($2::text[]))
                AND ($3::text IS NULL OR project=$3) AND ($4::text IS NULL OR status=$4)
                AND ($5::timestamptz IS NULL OR updated_at>=$5) AND (updated_at,id)<($6,$7)
                AND strpos(lower(project),lower($9))>0 ORDER BY updated_at DESC,id DESC LIMIT $8""",
                                    owner, projects, project, status, since, before_time, before_id, limit, query)
            return [record(r) for r in rows]

    @database
    async def remember_memory_scope(self, owner, sid, lease, scope):
        async with self.transaction() as (conn, tx):
            turn = await self._lease(conn, owner, sid, lease)
            if failed(turn):
                return tx.reject(turn)
            count = await conn.fetchval("SELECT count(*) FROM skirnir_sessions.session_memory_scopes WHERE owner=$1 AND session_id=$2", owner, sid)
            exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM skirnir_sessions.session_memory_scopes WHERE owner=$1 AND session_id=$2 AND project=$3)", owner, sid, scope['project'])
            if count >= 256 and not exists:
                return tx.reject(too_large("too many scope references"))
            await conn.execute("""INSERT INTO skirnir_sessions.session_memory_scopes(owner,session_id,project,payload)
                VALUES($1,$2,$3,$4) ON CONFLICT(owner,session_id,project) DO UPDATE SET payload=EXCLUDED.payload""", owner, sid, scope['project'], scope)

    @database
    async def import_session(self, owner, event, fingerprint, project, data_class, mode, metadata, messages, checkpoint):
        async with self.transaction() as (conn, tx):
            # Stable operation admission across router instances, before checking the
            # import key. No user-derived string is interpolated into SQL.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", json.dumps([owner, event]))
            previous = await conn.fetchrow("SELECT * FROM skirnir_sessions.handoff_imports WHERE owner=$1 AND event_id=$2", owner, event)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    return tx.reject(conflict("handoff event_id conflicts"))
                session = await self._get(conn, owner, previous['session_id'])
                return tx.reject(session) if failed(session) else (session, False)
            sid = uuid4()
            session = await conn.fetchrow("""INSERT INTO skirnir_sessions.sessions(owner,id,project,mode,data_class,metadata)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *""", owner, sid, project, mode, data_class, metadata)
            for index, message in enumerate(messages, 1):
                await conn.execute("""INSERT INTO skirnir_sessions.events(owner,session_id,seq,event_id,kind,payload)
                    VALUES($1,$2,$3,$4,'message',$5)""", owner, sid, index, f"handoff:{index}", message)
            if checkpoint:
                await conn.execute("""INSERT INTO skirnir_sessions.events(owner,session_id,seq,event_id,kind,version,payload)
                    VALUES($1,$2,$3,'handoff:checkpoint','checkpoint',1,$4)""", owner, sid, len(messages) + 1, checkpoint)
            await conn.execute("INSERT INTO skirnir_sessions.handoff_imports(owner,event_id,fingerprint,session_id) VALUES($1,$2,$3,$4)", owner, event, fingerprint, sid)
            return record(session), True
