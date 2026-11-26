"""The memory backend interface (ADR-0115): what the router needs from a long-term memory system, as typed values.

MemoryService (memory.py), the outbox worker (memory_outbox.py), the gateway (memory_gateway.py) and the status poll
(context_ops.py) work only with these values. An adapter maps its system's wire format onto them; hindsight.py is the
adapter for Hindsight and the only one so far (mem0 or a pgvector store would be further adapters). Scope and grant
checks, the content filter, provenance, recall budgets, duplicate handling and the deletion lifecycle stay in the router
and apply to every backend alike. Adapters live in this repository and are chosen by a fixed name in the configuration
(`hindsight.backend`, ADR-0118, ADR-0123); BACKENDS is the closed list of names.

A bank is the opaque namespace id the router derives per scope (`skirnir-` and 64 hex digits, memory.MemoryService.bank);
an adapter maps it onto its system's namespace and never accepts another form. `authorize` is a callable the adapter
calls right before it sends, inside its concurrency limit; False means the policy changed while waiting, and the call
returns POLICY_CHANGED without sending. Every call returns its value or a Problem (outcome.py) whose `kind` is one of
KINDS; adapters catch transport faults at their own boundary and nowhere else.
"""
from dataclasses import dataclass
from typing import Protocol

from .outcome import denied, invalid, unavailable

# Names in the configuration (hindsight.backend). A new adapter adds its name here and its constructor in open_backend.
BACKENDS = ("hindsight",)
# Why a backend gave no usable answer (Problem.kind); recalls report it as their state ("unavailable:<kind>").
KINDS = ("unavailable", "upstream_auth", "upstream_rejected", "protocol", "timeout", "transport", "remote_pending")
POLICY_CHANGED = denied("memory policy changed before dispatch")
# Upstream states of a retain operation the router acts on. An adapter maps its system's states onto these; any other
# string is passed on as reported (memory gateway `upstream_status`) and treated as still in progress.
PENDING, PROCESSING, COMPLETED, FAILED, CANCELLED = "pending", "processing", "completed", "failed", "cancelled"
NOT_FOUND = "not_found"   # the backend does not know the operation (any more)
UNKNOWN = "unknown"       # the backend answered without a readable state
OPERATION_STATUSES = (PENDING, PROCESSING, COMPLETED, FAILED, CANCELLED, NOT_FOUND, UNKNOWN)


def unavailable_as(kind):
    return unavailable("memory backend unavailable", kind if kind in KINDS else "unavailable")


@dataclass(frozen=True)
class Fact:
    """One memory unit as the router uses it. Everything in it is untrusted data from the backend; the router filters
    and bounds it before anything reaches a client or a model."""
    id: str
    text: str
    type: str | None = None          # "world", "experience" or "observation" where the backend distinguishes them
    score: float | None = None       # relevance from 0 to 1 as the backend ranked it (Hindsight: reranker); None = none
    sources: tuple = ()              # ids of the facts an observation was derived from (recall: only if all are text)
    document_id: str | None = None   # the retained document (= router operation id) the unit was extracted from
    metadata: dict | None = None     # metadata stored with the retain (the router's provenance record)
    created_at: str | None = None
    updated_at: str | None = None
    date: str | None = None          # event date the backend assigned (listing)


@dataclass(frozen=True)
class FactList:
    """A recall answer: the facts in the backend's ranking. bank_missing: the bank does not exist (nothing was ever
    retained under this scope and namespace key), a state of its own and not "nothing matched"."""
    facts: tuple
    bank_missing: bool = False


@dataclass(frozen=True)
class MemoryPage:
    """One page of a bank listing (curator view); total is None when the backend does not report it."""
    facts: tuple
    total: int | None = None


@dataclass(frozen=True)
class OpState:
    """Upstream state of a retain operation: one of OPERATION_STATUSES, or another state string as the backend reported
    it. COMPLETED and FAILED mean that this attempt can no longer write to the document. CANCELLED does not: Hindsight
    cancels running work cooperatively, and its worker may still write after the status changed (ADR-0152)."""
    status: str


class MemoryBackend(Protocol):
    """The calls the router makes. test/selftest_memory_backend.py holds the contract every adapter must pass."""

    async def health(self) -> bool:
        """Ask the backend's own health check (no bank, no content). True only for a healthy answer."""

    async def recall(self, bank, query, *, types=None, authorize=None):
        """FactList, or a Problem. types: None (backend default) or a list of fact types."""

    async def retain(self, bank, content, operation_id, *, document_id=None, confirmed, provenance=None, shared_observations=False,
                     context=None, timestamp=None, authorize=None):
        """None once the backend acknowledged the operation under exactly this operation id, or a Problem. Retaining the
        same operation id again is idempotent; document_id (default: the operation id) is what delete_document removes."""

    async def operation(self, bank, operation_id, *, authorize=None):
        """OpState of a retain operation (NOT_FOUND for one the backend does not know), or a Problem."""

    async def delete_document(self, bank, document_id, *, authorize=None):
        """None once the document and everything extracted from it is gone (also when it was gone already), or a Problem."""

    async def list(self, bank, *, kind=None, query=None, limit=20, offset=0, authorize=None):
        """MemoryPage (empty for a bank that does not exist), or a Problem."""

    async def close(self):
        """Release connections; called once at shutdown."""


def open_backend(cfg, token):
    """The configured adapter, or a Problem (unknown name, invalid endpoint). The configuration check refuses an unknown
    name already; this check keeps a programmatic configuration from opening anything else."""
    if cfg.backend == "hindsight":
        from . import hindsight   # imported here: hindsight.py itself builds on the types above
        return hindsight.open_backend(cfg, token)
    return invalid("memory backend must be one of: " + ", ".join(BACKENDS))
