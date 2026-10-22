"""Durable outbox and bounded lifecycle operations on the optional PostgreSQL pool.

Methods return their result or a Problem (outcome.py).
"""
from uuid import uuid4

from .outcome import conflict, failed, not_found, unavailable
from .session_types import database, record

OUTBOX_ERRORS = (None, "unavailable", "upstream_auth", "upstream_rejected", "protocol", "timeout", "transport", "remote_pending", "policy_changed",
                 "upstream_failed", "remote_unknown")
OUTBOX_FINAL = ('accepted', 'completed', 'dead', 'revoked', 'pending', 'deleted', 'delete_pending')
# Jobs whose payload is no longer needed. 'accepted' is not terminal: the job still waits for verified completion
# and may be resubmitted from its payload.
TERMINAL = ('completed', 'dead', 'revoked', 'cancelled', 'deleted')
OPERATION_NOT_FOUND = not_found("memory operation not found")


class ReliabilityStore:
    @database
    async def outbox_put(self, operation, scope, payload, fingerprint, capacity, owner_capacity=0):
        async with self.transaction() as (conn, tx):
            await conn.execute("SELECT pg_advisory_xact_lock(7365471202492)")
            old = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE operation_id=$1", operation)
            if old:
                if old["owner"] != scope["principal"] or old["fingerprint"] != fingerprint:
                    return tx.reject(conflict("event_id already used for different memory content"))
                return record(old)
            full = await self._outbox_capacity(conn, scope["principal"], capacity, owner_capacity)
            if failed(full):
                return tx.reject(full)
            row = await conn.fetchrow("""INSERT INTO skirnir_sessions.memory_outbox(operation_id,owner,project,scope,payload,fingerprint)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *""", operation, scope["principal"], scope["project"], scope, payload, fingerprint)
            return record(row)

    @database
    async def outbox_claim(self, lease_s, max_attempts):
        async with self.transaction() as (conn, tx):
            await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET state='dead',last_error='lease_expired',updated_at=clock_timestamp()
                WHERE operation_id IN (SELECT operation_id FROM skirnir_sessions.memory_outbox
                    WHERE state='processing' AND lease_until<clock_timestamp() AND attempts-retry_base>=$1
                    ORDER BY lease_until FOR UPDATE SKIP LOCKED LIMIT 100)""", max_attempts)
            # Sends (pending, delete_pending, expired processing) and verifications (accepted, due, not leased or lease expired).
            row = await conn.fetchrow("""SELECT * FROM skirnir_sessions.memory_outbox
                WHERE (state IN ('pending','delete_pending') AND available_at<=clock_timestamp()) OR (state='processing' AND lease_until<clock_timestamp())
                   OR (state='accepted' AND available_at<=clock_timestamp() AND (lease IS NULL OR lease_until<clock_timestamp()))
                ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1""")
            if not row:
                return None
            lease = uuid4()
            # A verification keeps the state 'accepted' and counts a check; a send becomes 'processing' and counts an attempt.
            row = await conn.fetchrow("""UPDATE skirnir_sessions.memory_outbox SET
                state=CASE WHEN state='accepted' THEN 'accepted' ELSE 'processing' END,
                attempts=CASE WHEN state='accepted' THEN attempts ELSE attempts+1 END,
                verify_checks=CASE WHEN state='accepted' THEN verify_checks+1 ELSE verify_checks END,
                lease=$2,lease_until=clock_timestamp()+$3*interval '1 second',updated_at=clock_timestamp()
                WHERE operation_id=$1 RETURNING *""", row["operation_id"], lease, lease_s)
            return record(row)

    @database
    async def outbox_finish(self, operation, lease, status, delay=0, error=None, upstream_attempts=None):
        """End a claimed send or verification. `upstream_attempts` (set when Hindsight reported the extraction as failed)
        moves the job to a new upstream operation id on its next send. None, or a Problem when the lease was lost."""
        # Callers pass module constants; anything else is a programming error, not an outcome.
        if error not in OUTBOX_ERRORS:
            raise ValueError("invalid diagnostic category")  # exception-ok: programming error (violated invariant)
        if status not in OUTBOX_FINAL:
            raise ValueError("invalid outbox completion")  # exception-ok: programming error (violated invariant)
        async with self.connection() as conn:
            # First acceptance after a send starts the verification clock; a verification that keeps 'accepted' does not.
            result = await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET state=$3,lease=NULL,lease_until=NULL,
                available_at=clock_timestamp()+$4*interval '1 second',updated_at=clock_timestamp(),
                last_error=$5,payload=CASE WHEN $3 IN ('deleted','revoked') THEN NULL ELSE payload END,
                accepted_at=CASE WHEN $3='accepted' AND state='processing' THEN clock_timestamp() ELSE accepted_at END,
                verify_checks=CASE WHEN $3='accepted' AND state='processing' THEN 0 ELSE verify_checks END,
                retry_base=CASE WHEN $6::integer IS NOT NULL THEN attempts ELSE retry_base END,
                upstream_attempts=COALESCE($6,upstream_attempts)
                WHERE operation_id=$1 AND lease=$2 AND state IN ('processing','accepted') AND lease_until>clock_timestamp()""",
                operation, lease, status, delay, error, upstream_attempts)
            return conflict("outbox lease lost") if result == 'UPDATE 0' else None

    @database
    async def outbox_get(self, owner, operation):
        async with self.connection() as conn:
            row = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE owner=$1 AND operation_id=$2", owner, operation)
            return record(row) if row else OPERATION_NOT_FOUND

    @database
    async def outbox_find(self, operation):
        """The operation whoever owns it - only for curating a shared scope (memory_gateway.curation_target checks the
        curator's grant and the bank before the row is used)."""
        async with self.connection() as conn:
            row = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE operation_id=$1", operation)
            return record(row) if row else OPERATION_NOT_FOUND

    @database
    async def outbox_cancel(self, owner, operation):
        async with self.transaction() as (conn, tx):
            row = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE owner=$1 AND operation_id=$2 FOR UPDATE", owner, operation)
            if not row:
                return tx.reject(OPERATION_NOT_FOUND)
            if row["state"] not in ('pending', 'dead', 'revoked', 'cancelled') or (row["attempts"] > 0 and row["state"] != 'cancelled'):
                return tx.reject(conflict("operation may already be in Hindsight; cancellation is not a deletion guarantee"))
            await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET state='cancelled',payload=NULL,updated_at=clock_timestamp()
                WHERE operation_id=$1""", operation)

    @database
    async def outbox_counts(self):
        async with self.connection() as conn:
            rows = await conn.fetch("SELECT state,jobs AS count FROM skirnir_sessions.outbox_totals WHERE jobs>0")
            return {r["state"]: r["count"] for r in rows}

    @database
    async def cleanup(self, session_days, archived_days, outbox_days):
        async with self.transaction() as (conn, tx):
            rows = await conn.fetch("""SELECT owner,id FROM skirnir_sessions.sessions s
                WHERE (($1>0 AND updated_at<clock_timestamp()-$1*interval '1 day') OR
                       ($2>0 AND status='archived' AND updated_at<clock_timestamp()-$2*interval '1 day'))
                AND NOT EXISTS(SELECT 1 FROM skirnir_sessions.turns t WHERE t.owner=s.owner AND t.session_id=s.id
                    AND state='pending' AND lease_until>clock_timestamp())
                ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 100""", session_days, archived_days)
            for row in rows:
                await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2", row['owner'], row['id'])
            # Keep idempotency tombstones. Removing IDs could resurrect deleted knowledge.
            result = await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET payload=NULL,scope=jsonb_build_object('__bank',scope->>'__bank')
                WHERE operation_id IN (SELECT operation_id FROM skirnir_sessions.memory_outbox
                    WHERE $1>0 AND payload IS NOT NULL AND state IN ('completed','dead','revoked','cancelled','deleted')
                    AND updated_at<clock_timestamp()-$1*interval '1 day' ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 100)""", outbox_days)
            return {"sessions": len(rows), "outbox_payloads": int(result.split()[-1])}

    @database
    async def outbox_delete(self, owner, operation, scope):
        async with self.transaction() as (conn, tx):
            row = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE owner=$1 AND operation_id=$2 FOR UPDATE", owner, operation)
            if not row:
                return tx.reject(OPERATION_NOT_FOUND)
            if row["state"] == 'processing':
                return tx.reject(conflict("operation is in flight; retry later"))
            if row["state"] in ('deleted', 'delete_pending'):
                return None
            if row["attempts"] == 0 or row["state"] == 'cancelled':
                await conn.execute("UPDATE skirnir_sessions.memory_outbox SET state='deleted',payload=NULL,updated_at=clock_timestamp() WHERE operation_id=$1", operation)
            else:
                await conn.execute("""UPDATE skirnir_sessions.memory_outbox SET state='delete_pending',scope=$2,retry_base=attempts,
                    delete_requested=true,available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE operation_id=$1""", operation, scope)

    async def _outbox_capacity(self, conn, owner, capacity, owner_capacity):
        size = await conn.fetchval("SELECT COALESCE(sum(jobs),0) FROM skirnir_sessions.outbox_totals WHERE state IN ('pending','processing','delete_pending')")
        if size >= capacity:
            return unavailable("memory outbox full")
        if owner_capacity:
            size = await conn.fetchval("SELECT count(*) FROM skirnir_sessions.memory_outbox WHERE owner=$1 AND state IN ('pending','processing','delete_pending')", owner)
            if size >= owner_capacity:
                return unavailable("client outbox capacity reached")
        return None

    @database
    async def outbox_oldest(self):
        async with self.connection() as conn:
            value = await conn.fetchval("SELECT EXTRACT(EPOCH FROM clock_timestamp()-created_at) FROM skirnir_sessions.memory_outbox WHERE state IN ('pending','processing','delete_pending') ORDER BY created_at LIMIT 1")
            return max(0, float(value or 0))

    @database
    async def outbox_retry(self, owner, operation, retry_id, scope, capacity, owner_capacity=0):
        async with self.transaction() as (conn, tx):
            await conn.execute("SELECT pg_advisory_xact_lock(7365471202492)")
            row = await conn.fetchrow("SELECT * FROM skirnir_sessions.memory_outbox WHERE owner=$1 AND operation_id=$2 FOR UPDATE", owner, operation)
            if not row:
                return tx.reject(OPERATION_NOT_FOUND)
            if retry_id in row['retry_ids']:
                return record(row)
            if row['state'] != 'dead' or row['retry_rounds'] >= 3 or (row['payload'] is None and not row['delete_requested']):
                return tx.reject(conflict("only retained dead-letter payloads or failed deletions can be retried; maximum three rounds"))
            if row['scope'].get('__bank') != scope.get('__bank'):
                return tx.reject(conflict("operation bank binding changed"))
            full = await self._outbox_capacity(conn, owner, capacity, owner_capacity)
            if failed(full):
                return tx.reject(full)
            return record(await conn.fetchrow("""UPDATE skirnir_sessions.memory_outbox SET state=$3,scope=$4,
                retry_ids=retry_ids || jsonb_build_array($5::text),retry_base=attempts,upstream_base=upstream_attempts,verify_checks=0,retry_rounds=retry_rounds+1,last_error=NULL,
                available_at=clock_timestamp(),updated_at=clock_timestamp(),lease=NULL,lease_until=NULL
                WHERE owner=$1 AND operation_id=$2 RETURNING *""", owner, operation,
                'delete_pending' if row['delete_requested'] else 'pending', scope, retry_id))
