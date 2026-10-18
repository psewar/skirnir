"""Managed turns and immutable context versions on the existing session store.

Short owner-scoped transactions; no database connection is held during inference.
A renewable lease serializes writers across router processes. Failed turns retain
raw input and may be retried with the same event ID until a newer turn is written.
"""
import hashlib
import json
from uuid import uuid4

from .context_manager import tool_delta
from .session_types import SessionCapacity, SessionConflict, record


class ManagedStore:
    async def _idle(self, conn, owner, sid):
        pending = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM skirnir_sessions.turns
            WHERE owner=$1 AND session_id=$2 AND state='pending' AND lease_until>clock_timestamp())""", owner, sid)
        if pending:
            raise SessionConflict("session has an active turn")

    async def _lease(self, conn, owner, sid, lease):
        session = await self._get(conn, owner, sid, lock=True)
        row = await conn.fetchrow("""SELECT * FROM skirnir_sessions.turns WHERE owner=$1 AND session_id=$2
            AND lease=$3 AND state='pending' AND lease_until>clock_timestamp()""", owner, sid, lease)
        if row is None or session["status"] != "active":
            raise SessionConflict("managed turn lease expired or session changed")
        return row

    async def claim(self, owner, session_id, event_id, fingerprint, messages, cfg):
        async with self.connection() as conn, conn.transaction():
            session = await self._get(conn, owner, session_id, lock=True)
            if session["mode"] != "skirnir-managed" or session["status"] != "active":
                raise SessionConflict("session is not active and skirnir-managed")
            await conn.execute("""UPDATE skirnir_sessions.turns SET state='failed' WHERE owner=$1
                AND session_id=$2 AND state='pending' AND lease_until<=clock_timestamp()""", owner, session_id)
            await self._idle(conn, owner, session_id)
            old = await conn.fetchrow("SELECT * FROM skirnir_sessions.turns WHERE owner=$1 AND session_id=$2 AND event_id=$3",
                                      owner, session_id, event_id)
            seq = await conn.fetchval("SELECT COALESCE(MAX(seq),0) FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2", owner, session_id)
            start_seq = seq + 1
            if old:
                start_seq = old["input_start_seq"]
                if old["fingerprint"] != fingerprint:
                    raise SessionConflict("event_id already used with different input")
                if old["state"] == "completed":
                    raise SessionConflict("turn already completed; read persisted messages")
                newer = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM skirnir_sessions.events WHERE owner=$1
                    AND session_id=$2 AND kind='message' AND seq>$3)""", owner, session_id, old["input_end_seq"])
                if newer:
                    raise SessionConflict("cannot retry an old turn after newer messages")
                seq = old["input_end_seq"]
            else:
                # Bound the reconstructed working set BEFORE appending, within this transaction.
                context = await self._context(conn, owner, session_id, cfg, extra=messages)
                messages = tool_delta(context, messages)
                prefix = "managed:" + hashlib.sha256(event_id.encode()).hexdigest()
                for index, message in enumerate(messages):
                    seq += 1
                    await conn.execute("""INSERT INTO skirnir_sessions.events(owner,session_id,seq,event_id,kind,payload)
                        VALUES($1,$2,$3,$4,'message',$5)""", owner, session_id, seq, f"{prefix}:{index}", message)
            lease = uuid4()
            await conn.execute("""INSERT INTO skirnir_sessions.turns(owner,session_id,event_id,fingerprint,state,lease,lease_until,input_end_seq,input_start_seq)
                VALUES($1,$2,$3,$4,'pending',$5,clock_timestamp()+$6*interval '1 second',$7,$8)
                ON CONFLICT(owner,session_id,event_id) DO UPDATE SET state='pending', lease=EXCLUDED.lease, lease_until=EXCLUDED.lease_until""",
                               owner, session_id, event_id, fingerprint, lease, cfg.lease_s, seq, start_seq)
            return {"lease": lease, "input_end_seq": seq}

    async def renew(self, owner, session_id, lease, seconds):
        async with self.connection() as conn, conn.transaction():
            await self._lease(conn, owner, session_id, lease)
            await conn.execute("""UPDATE skirnir_sessions.turns SET lease_until=clock_timestamp()+$4*interval '1 second'
                WHERE owner=$1 AND session_id=$2 AND lease=$3""", owner, session_id, lease, seconds)

    async def fail_turn(self, owner, session_id, lease):
        async with self.connection() as conn:
            await conn.execute("""UPDATE skirnir_sessions.turns SET state='failed' WHERE owner=$1 AND session_id=$2
                AND lease=$3 AND state='pending'""", owner, session_id, lease)

    async def finish_turn(self, owner, session_id, lease, message):
        async with self.connection() as conn, conn.transaction():
            turn = await self._lease(conn, owner, session_id, lease)
            seq = await conn.fetchval("SELECT COALESCE(MAX(seq),0)+1 FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2", owner, session_id)
            eid = "managed:" + hashlib.sha256(turn["event_id"].encode()).hexdigest() + ":answer"
            await conn.execute("""INSERT INTO skirnir_sessions.events(owner,session_id,seq,event_id,kind,payload)
                VALUES($1,$2,$3,$4,'message',$5)""", owner, session_id, seq, eid, message)
            await conn.execute("UPDATE skirnir_sessions.turns SET state='completed' WHERE owner=$1 AND session_id=$2 AND lease=$3", owner, session_id, lease)

    async def _context(self, conn, owner, sid, cfg, extra=()):
        session = await self._get(conn, owner, sid, lock=True)
        compaction = await conn.fetchrow("""SELECT * FROM skirnir_sessions.compactions WHERE owner=$1 AND session_id=$2
            ORDER BY version DESC LIMIT 1""", owner, sid)
        checkpoint = await conn.fetchrow("""SELECT * FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2
            AND kind='checkpoint' ORDER BY version DESC LIMIT 1""", owner, sid)
        compaction = record(compaction) if compaction else None
        checkpoint = record(checkpoint) if checkpoint else None
        total = len(json.dumps([compaction, checkpoint], ensure_ascii=False).encode())
        messages = []
        pin_count = len(compaction["payload"]["pinned"]) if compaction else 0
        after = compaction["through_seq"] if compaction else 0
        async for row in conn.cursor("""SELECT * FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2
                AND kind='message' AND seq>$3 ORDER BY seq""", owner, sid, after):
            total += len(json.dumps(row["payload"], ensure_ascii=False).encode())
            messages.append(record(row))
            if total > cfg.max_history_bytes or len(messages) + len(extra) + pin_count > cfg.max_history_messages:
                raise SessionCapacity("working history limit exceeded; export history or start a new session")
        total += sum(len(json.dumps(m, ensure_ascii=False).encode()) for m in extra)
        if total > cfg.max_history_bytes:
            raise SessionCapacity("working history limit exceeded")
        unresolved = await conn.fetch("""SELECT input_start_seq,input_end_seq FROM skirnir_sessions.turns
            WHERE owner=$1 AND session_id=$2 AND state IN ('pending','failed') AND input_end_seq>$3""", owner, sid, after)
        protected = [row["seq"] for row in messages if any(t["input_start_seq"] <= row["seq"] <= t["input_end_seq"] for t in unresolved)]
        turn = await conn.fetchrow("""SELECT event_id,state,input_end_seq,created_at FROM skirnir_sessions.turns
            WHERE owner=$1 AND session_id=$2 ORDER BY input_end_seq DESC,created_at DESC LIMIT 1""", owner, sid)
        return {"session": session, "compaction": compaction, "checkpoint": checkpoint, "messages": messages,
                "last_turn": record(turn) if turn else None, "protected_sequences": protected}

    async def context(self, owner, session_id, cfg):
        async with self.connection() as conn, conn.transaction():
            return await self._context(conn, owner, session_id, cfg)

    async def compact(self, owner, session_id, lease, through_seq, payload):
        async with self.connection() as conn, conn.transaction():
            await self._lease(conn, owner, session_id, lease)
            last = await conn.fetchrow("""SELECT version,through_seq FROM skirnir_sessions.compactions
                WHERE owner=$1 AND session_id=$2 ORDER BY version DESC LIMIT 1""", owner, session_id)
            if last and through_seq < last["through_seq"]:
                raise SessionConflict("compaction cannot move backwards")
            version = last["version"] + 1 if last else 1
            row = await conn.fetchrow("""INSERT INTO skirnir_sessions.compactions(owner,session_id,version,through_seq,payload)
                VALUES($1,$2,$3,$4,$5) RETURNING *""", owner, session_id, version, through_seq, payload)
            return record(row)

    async def compactions(self, owner, session_id, after, limit):
        async with self.connection() as conn, conn.transaction():
            await self._get(conn, owner, session_id)
            rows = await conn.fetch("""SELECT * FROM skirnir_sessions.compactions WHERE owner=$1 AND session_id=$2
                AND version>$3 ORDER BY version LIMIT $4""", owner, session_id, after, limit)
            return [record(row) for row in rows]

    async def search(self, owner, session_id, query, after, limit):
        async with self.connection() as conn, conn.transaction():
            await self._get(conn, owner, session_id)
            rows = await conn.fetch("""SELECT * FROM skirnir_sessions.events WHERE owner=$1 AND session_id=$2
                AND seq>$3 AND strpos(lower(payload::text),lower($4))>0 ORDER BY seq LIMIT $5""", owner, session_id, after, query, limit)
            return [record(row) for row in rows]
