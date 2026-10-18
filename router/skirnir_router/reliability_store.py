"""Durable outbox and bounded lifecycle operations on the optional PostgreSQL pool."""
from uuid import uuid4

from .session_types import SessionConflict, SessionNotFound, SessionCapacity, record


class ReliabilityStore:
    async def outbox_put(self, operation, scope, payload, fingerprint, capacity):
        async with self.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7365471202492)")
            old = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE operation_id=$1", operation)
            if old:
                if old["owner"] != scope["principal"] or old["fingerprint"] != fingerprint:
                    raise SessionConflict("event_id already used for different memory content")
                return record(old)
            size = await conn.fetchval("SELECT count(*) FROM skirnir_sessions.memory_outbox WHERE state IN ('pending','processing','delete_pending')")
            if size >= capacity:
                raise SessionCapacity("memory outbox full")
            row = await conn.fetchrow("""INSERT INTO skirnir_sessions.memory_outbox(operation_id,owner,project,scope,payload,fingerprint)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *""", operation, scope["principal"], scope["project"], scope, payload, fingerprint)
            return record(row)

    async def outbox_claim(self, lease_s, max_attempts):
        async with self.connection() as conn, conn.transaction():
            await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET state='dead',updated_at=clock_timestamp()
                WHERE state='processing' AND lease_until<clock_timestamp() AND attempts>=$1""", max_attempts)
            row = await conn.fetchrow("""SELECT * FROM skirnir_sessions.memory_outbox
                WHERE (state IN ('pending','delete_pending') AND available_at<=clock_timestamp()) OR (state='processing' AND lease_until<clock_timestamp())
                ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1""")
            if not row:
                return None
            lease = uuid4()
            row = await conn.fetchrow("""UPDATE skirnir_sessions.memory_outbox SET state='processing',attempts=attempts+1,
                lease=$2,lease_until=clock_timestamp()+$3*interval '1 second',updated_at=clock_timestamp()
                WHERE operation_id=$1 RETURNING *""", row["operation_id"], lease, lease_s)
            return record(row)

    async def outbox_finish(self, operation, lease, status, delay=0):
        if status not in ('accepted', 'dead', 'revoked', 'pending', 'deleted', 'delete_pending'):
            raise ValueError("invalid outbox completion")
        async with self.connection() as conn:
            result = await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET state=$3,lease=NULL,lease_until=NULL,
                available_at=clock_timestamp()+$4*interval '1 second',updated_at=clock_timestamp(),
                payload=CASE WHEN $3 IN ('deleted','revoked') THEN NULL ELSE payload END
                WHERE operation_id=$1 AND lease=$2 AND state='processing' AND lease_until>clock_timestamp()""", operation, lease, status, delay)
            if result == 'UPDATE 0':
                raise SessionConflict("outbox lease lost")

    async def outbox_get(self, owner, operation):
        async with self.connection() as conn:
            row = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE owner=$1 AND operation_id=$2", owner, operation)
            if not row:
                raise SessionNotFound
            return record(row)

    async def outbox_cancel(self, owner, operation):
        async with self.connection() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE owner=$1 AND operation_id=$2 FOR UPDATE", owner, operation)
            if not row:
                raise SessionNotFound
            if row["state"] not in ('pending', 'dead', 'revoked', 'cancelled') or (row["attempts"] > 0 and row["state"] != 'cancelled'):
                raise SessionConflict("operation may already be in Hindsight; cancellation is not a deletion guarantee")
            await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET state='cancelled',payload=NULL,updated_at=clock_timestamp()
                WHERE operation_id=$1""", operation)

    async def outbox_counts(self):
        async with self.connection() as conn:
            rows = await conn.fetch("SELECT state,count(*) AS count FROM skirnir_sessions.memory_outbox GROUP BY state")
            return {r["state"]: r["count"] for r in rows}

    async def cleanup(self, session_days, archived_days, outbox_days):
        async with self.connection() as conn, conn.transaction():
            rows = await conn.fetch("""SELECT owner,id FROM skirnir_sessions.sessions s
                WHERE (($1>0 AND updated_at<clock_timestamp()-$1*interval '1 day') OR
                       ($2>0 AND status='archived' AND updated_at<clock_timestamp()-$2*interval '1 day'))
                AND NOT EXISTS(SELECT 1 FROM skirnir_sessions.turns t WHERE t.owner=s.owner AND t.session_id=s.id
                    AND state='pending' AND lease_until>clock_timestamp())
                ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 100""", session_days, archived_days)
            for row in rows:
                await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2", row['owner'], row['id'])
            # Keep idempotency tombstones. Removing IDs could resurrect deleted knowledge.
            result = await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET payload=NULL
                WHERE operation_id IN (SELECT operation_id FROM skirnir_sessions.memory_outbox
                    WHERE $1>0 AND payload IS NOT NULL AND state IN ('accepted','dead','revoked','cancelled','deleted')
                    AND updated_at<clock_timestamp()-$1*interval '1 day' ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 100)""", outbox_days)
            return {"sessions": len(rows), "outbox_payloads": int(result.split()[-1])}


    async def outbox_delete(self, owner, operation, scope):
        async with self.connection() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE owner=$1 AND operation_id=$2 FOR UPDATE", owner, operation)
            if not row:
                raise SessionNotFound
            if row["state"] == 'processing':
                raise SessionConflict("operation is in flight; retry later")
            if row["state"] in ('deleted', 'delete_pending'):
                return
            if row["attempts"] == 0 or row["state"] == 'cancelled':
                await conn.execute("UPDATE skirnir_sessions.memory_outbox SET state='deleted',payload=NULL,updated_at=clock_timestamp() WHERE operation_id=$1", operation)
            else:
                await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET state='delete_pending',scope=$2,attempts=0,
                    delete_requested=true,available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE operation_id=$1""", operation, scope)
