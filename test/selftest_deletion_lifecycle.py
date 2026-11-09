"""Self-test (ADR-0076, TM-12) without a database: deletion journal, operation header, reconcile endpoint, bounded
decision capture, posture facts.

  * the journal records ids only, survives torn and malformed lines, dedupes, and its replay hands exactly the recorded
    sessions and operations to the store; an unreadable journal or a store failure is a Problem (startup aborts);
  * an inference response that may be retained names its memory operation (X-Skirnir-Memory-Operation);
  * the reconcile endpoint deletes each listed document again, counts unreachable ones, pages with `next` and validates
    its input;
  * the decision capture keeps at most one rotated predecessor;
  * the posture report names retention and deletion choices that keep content.
    python test/selftest_deletion_lifecycle.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from uuid import UUID, uuid4

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "router"))
from skirnir_router import context_ops, deletion_journal, memory, posture, request, state  # noqa: E402
from skirnir_router.decision import capture  # noqa: E402
from skirnir_router.outcome import failed, unavailable  # noqa: E402
from skirnir_router.session_config import SessionConfig, parse_session_config  # noqa: E402

FAILS = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


class FakeStore:
    def __init__(self, result=None):
        self.calls, self.result = [], result

    async def replay_deletions(self, sessions, operations, with_memory):
        self.calls.append((sessions, operations, with_memory))
        return self.result if self.result is not None else {"sessions": len(sessions), "operations": len(operations)}


tmp = tempfile.mkdtemp()
state.CFG = types.SimpleNamespace(path=os.path.join(tmp, "config.yaml"))
check(deletion_journal.path() == os.path.join(tmp, "deletions.jsonl"), "journal lives next to config.yaml")

s1, op1 = uuid4(), uuid4()
deletion_journal.record("session", "alice", s1)
deletion_journal.record("operation", "bob", op1)
deletion_journal.record("session", "alice", s1)
deletion_journal.record("bogus", "alice", s1)   # unknown kinds are not written
with open(deletion_journal.path(), "a", encoding="utf-8") as f:
    f.write('{"kind":"session","owner":"x","id":"not-a-uuid"}\n{"kind":"operation","owner":"y","id":"' + str(uuid4())[:20] + "\n")
raw = open(deletion_journal.path(), encoding="utf-8").read()
check(all(json.loads(x).keys() == {"ts", "kind", "owner", "id"} for x in raw.splitlines()[:3]) and "bogus" not in raw,
      "journal lines hold time, kind, owner and id only")
sessions, operations, skipped = deletion_journal.parse(raw.splitlines())
check(sessions == [("alice", s1)] and operations == [("bob", op1)] and skipped == 2,
      f"parse: deduped, malformed and torn lines skipped ({sessions}, {operations}, {skipped})")

store = FakeStore()
result = asyncio.run(deletion_journal.replay(store, True))
check(result == {"sessions": 1, "operations": 1} and store.calls == [([("alice", s1)], [("bob", op1)], True)],
      "replay hands the journal to the store with the option")
check(failed(asyncio.run(deletion_journal.replay(FakeStore(unavailable("db")), False))), "a store failure is a Problem")
os.remove(deletion_journal.path())
os.makedirs(deletion_journal.path())   # a folder where the file should be: unreadable
check(failed(asyncio.run(deletion_journal.replay(FakeStore(), False))), "an unreadable journal is a Problem (nothing may send)")
os.rmdir(deletion_journal.path())
check(asyncio.run(deletion_journal.replay(FakeStore(), False)) == {"sessions": 0, "operations": 0}, "no journal: nothing to do")

info = {"request_id": "r", "node": "n", "model": "m", "tier": 0, "warm": True, "memory_operation": str(op1)}
check(request.headers_for(info).get("X-Skirnir-Memory-Operation") == str(op1), "response names the memory operation")
check("X-Skirnir-Memory-Operation" not in request.headers_for({**info, "memory_operation": None}), "no operation, no header")


class FakeClient:
    def __init__(self):
        self.deleted = []

    async def delete_document(self, bank, document_id):
        self.deleted.append((bank, document_id))
        return unavailable("down") if bank == "down" else {"deleted": True}


class FakeOutbox:
    def __init__(self, rows):
        self.rows = rows

    async def outbox_deleted_documents(self, after, limit):
        return [r for r in self.rows if r[0] > after][:limit]


ids = sorted(uuid4() for _ in range(3))
service = types.SimpleNamespace(outbox=FakeOutbox([(ids[0], "bank-a"), (ids[1], "down"), (ids[2], None)]),
                                client=FakeClient(), active=lambda: True)


class FakeRequest(dict):
    def __init__(self, body):
        super().__init__()
        self.app = {context_ops.API_KEY: {memory.SERVICE_KEY: service}}
        self.can_read_body = body is not None
        self._body = body

    async def json(self):
        return self._body

    async def read(self):
        return json.dumps(self._body).encode()

    @property
    def content_length(self):
        return len(json.dumps(self._body))


def call(body):
    resp = asyncio.run(context_ops.handle_reconcile_deletions(FakeRequest(body)))
    return resp.status, json.loads(resp.body)


status, body = call({"limit": 2})
check(status == 200 and body == {"checked": 2, "deleted": 1, "unavailable": 1, "next": str(ids[1])},
      f"reconcile: first page, one deleted, one unreachable ({body})")
status, body = call({"after": str(ids[1]), "limit": 2})
check(status == 200 and body == {"checked": 1, "deleted": 0, "unavailable": 1, "next": None},
      f"reconcile: last page, a row without bank counts as unreachable ({body})")
check(service.client.deleted == [("bank-a", str(ids[0])), ("down", str(ids[1]))], "reconcile deletes by operation id")
check(call({"limit": 0})[0] == 400 and call({"after": "x"})[0] == 400, "reconcile: invalid input refused")
service.outbox = None
check(call(None)[0] == 404, "reconcile without a durable outbox: 404")

old_max = capture.MAX_BYTES
capture.MAX_BYTES = 200
cap = capture.Capture({"enabled": True, "path": os.path.join(tmp, "decisions.jsonl"), "clients": ["c"], "anonymize": False})
for i in range(20):
    cap.record("context " + str(i), ["a", "b"], "a", "test", "c", f"r{i}")
capture.MAX_BYTES = old_max
files = sorted(f for f in os.listdir(tmp) if f.startswith("decisions.jsonl"))
check(files == ["decisions.jsonl", "decisions.jsonl.1"] and os.path.getsize(os.path.join(tmp, "decisions.jsonl")) <= 400,
      f"decision capture keeps one rotated predecessor ({files})")

cfg = types.SimpleNamespace(client_auth={}, session_store=SessionConfig(enabled=True), hindsight=types.SimpleNamespace(
    enabled=True, outbox_enabled=True, outbox_retention_days=0))
codes = {f.code for f in posture.findings(cfg, {})}
check({"session_retention_off", "memory_kept_on_session_delete", "outbox_payload_retention_off"} <= codes, f"posture names the choices ({codes})")
cfg = types.SimpleNamespace(client_auth={}, session_store=SessionConfig(enabled=True, retention_days=30, delete_memory_with_session=True),
                            hindsight=types.SimpleNamespace(enabled=True, outbox_enabled=True, outbox_retention_days=30))
codes = {f.code for f in posture.findings(cfg, {})}
check(not ({"session_retention_off", "memory_kept_on_session_delete", "outbox_payload_retention_off"} & codes),
      "posture is quiet when retention and deletion are set")
check(failed(parse_session_config({"delete_memory_with_session": "yes"})), "the option must be a boolean")
check(UUID(int=0) < ids[0], "uuid ordering used for paging")

if FAILS:
    print(f"{len(FAILS)} failure(s)")
    sys.exit(1)
print("selftest_deletion_lifecycle: all passed")
