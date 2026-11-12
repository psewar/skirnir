"""Recalls skipped because the request carries tools, as a state per client (ADR-0089).

ADR-0059 skips automatic recall into a request with tools unless the project grant sets `recall_into_tools: true`. Each
such request already says so (`X-Skirnir-Memory-State: skipped:tools`, journal, an aggregate counter), but a client
that never reads the header - a Home Assistant entry whose tool API was switched on, say - silently loses its knowledge.
This module keeps the fact the operator needs: which client's recalls have all been skipped for tools, since when.
The clients are named only in /admin/context behind the admin login; the log line and the gauge only count them.

A run starts with a client's first skipped recall and ends with its next recall that passes the tools check (no tools,
or the grant opts in). It is reported while its last skipped request is at most RUN_WINDOW_S old, so a client that
stopped asking for recall drops out on its own. The first request of a run logs one warning, at most one per client and
WARN_INTERVAL_S however often its requests switch. State lives in memory: a restart starts over, and the next skipped
request reports the client again.

Only client and project names leave this module, through view() (both come from the configuration); no request
content does.
"""
import time

from .common import log

RUN_WINDOW_S = 3600      # a run is reported while its last skipped request is at most this old
WARN_INTERVAL_S = 3600   # at most one warning line per client within this time
_RUNS = {}               # client -> {"project", "since", "last", "requests"}
_WARNED = {}             # client -> time of its last warning line


def skipped(client, project, now=None):
    """Records that a recall of `client` for `project` was skipped because its request carries tools. Returns the
    client's current run; `warned` says whether this request wrote the warning line."""
    now = time.time() if now is None else now
    run = _RUNS.get(client)
    warned = False
    if run is None or now - run["last"] > RUN_WINDOW_S:
        run = _RUNS[client] = {"project": project, "since": now, "last": now, "requests": 0}
        if client not in _WARNED or now - _WARNED[client] >= WARN_INTERVAL_S:
            _WARNED[client] = now
            warned = True
            # Memory logs name no principal and no project (design/hindsight-memory.md); /admin/context does, behind login.
            log.warning("memory: a client configured for recall now gets no recalled facts: its requests carry tools and "
                        "the grant does not set recall_into_tools (skipped:tools, ADR-0059); %d client(s) in this state. "
                        "Next: /admin/context (recall_skipped_for_tools) or the Overview names them; if one should get "
                        "facts, set recall_into_tools: true on its grant, which lets facts reach a model that can call "
                        "tools; otherwise nothing to do.", len(_RUNS))
    run.update(project=project, last=now, requests=run["requests"] + 1)
    return {**run, "warned": warned}


def passed(client):
    """A recall of `client` passed the tools check (no tools, or the grant opts in): its run ends. Returns whether one
    was running."""
    return _RUNS.pop(client, None) is not None


def view(now=None):
    """{client: {"project", "since", "last", "requests"}} for the runs whose last skipped request is at most
    RUN_WINDOW_S old (times in epoch seconds). Older runs are dropped."""
    now = time.time() if now is None else now
    for client in [c for c, run in _RUNS.items() if now - run["last"] > RUN_WINDOW_S]:
        del _RUNS[client]
    return {c: {**run, "since": int(run["since"]), "last": int(run["last"])} for c, run in sorted(_RUNS.items())}


def reset():
    """Forgets all runs and warnings (tests)."""
    _RUNS.clear()
    _WARNED.clear()
