"""Problems as structured records (router 0.35.0, ADR-0101).

A problem is a condition the operator should know about although the router keeps running: no node online, a role
that cannot be served, a silent agent, a model partly in system RAM, the GPU guard, failed or stalled updates, the cloud
budget. Until router 0.34 the only channel was a list of German sentences in the Home Assistant state. This module
computes the problems independently of MQTT, as records with a stable code; Home Assistant, `/metrics`,
`GET /admin/problems` and the web UI all read them from here.

Record: {"code", "severity", "node", "subject", "params", "message"}
- `code` is fixed snake_case English from CATALOGUE; it never carries a value.
- `severity` is fixed per code: `critical` when requests fail or a configured tier drops out right now, `warning` when
  the router still serves but degraded or blind (ADR-0101).
- `node` is the node the problem is about (None if none), `subject` the second identity where one code can occur several
  times on the same node or without one: the role, the cloud provider, the model. Both come from the configuration, the
  agent registry or the models a node reports, so the label set of `skirnir_problem` stays bounded.
- `params` are the values of the message; free text from elsewhere (an agent's update message, the version check error)
  stays here and never becomes a metric label.
- `message` is the text in the requested language (`router.problem_language`, default `de` = the texts Home Assistant
  automations have matched since router 0.1).

Not to be confused with `outcome.Problem`, the expected outcome of a single request.
"""

import time

from . import state

SEVERITIES = ("warning", "critical")
LANGUAGES = ("de", "en")

# code -> (severity, {language: template}). Templates use str.format with the record's params; the values are substituted
# once and never expanded again, so a reported name cannot inject a placeholder. The German texts are byte for byte those
# of router 0.34 (Home Assistant automations match them).
CATALOGUE = {
    "no_node_online": ("critical", {
        "de": "kein Knoten online",
        "en": "no node online"}),
    "role_not_servable": ("critical", {
        "de": "Rolle {role} nicht bedienbar",
        "en": "role {role} cannot be served"}),
    "agent_heartbeat_missing": ("warning", {
        "de": "Knoten {node}: kein Agent-Heartbeat seit {minutes} min",
        "en": "node {node}: no agent heartbeat for {minutes} min"}),
    "ram_spill": ("warning", {
        "de": "Knoten {node}: {model} liegt zu {gib:.1f} GiB im Arbeitsspeicher (rechnet langsam auf der CPU)",
        "en": "node {node}: {model} has {gib:.1f} GiB in system RAM (runs slowly on the CPU)"}),
    "gpu_guard": ("warning", {
        "de": "Knoten {node}: GPU-Schutz {guard_state} ({reason})",
        "en": "node {node}: GPU guard {guard_state} ({reason})"}),
    "agent_update_failed": ("warning", {
        "de": "Agent-Update auf {node} fehlgeschlagen: {message}",
        "en": "agent update on {node} failed: {message}"}),
    "agent_update_stalled": ("warning", {
        "de": "Agent-Update auf {node} haengt ({message})",
        "en": "agent update on {node} is stalled ({message})"}),
    "ollama_update_failed": ("warning", {
        "de": "Ollama-Update auf {node} fehlgeschlagen: {message}",
        "en": "Ollama update on {node} failed: {message}"}),
    "ollama_update_stalled": ("warning", {
        "de": "Ollama-Update auf {node} haengt ({message})",
        "en": "Ollama update on {node} is stalled ({message})"}),
    "ollama_version_check_failing": ("warning", {
        "de": "Ollama-Versionspruefung scheitert: {error}",
        "en": "Ollama version check is failing: {error}"}),
    "cloud_budget_exhausted": ("critical", {
        "de": "Cloud {provider}: Monatsbudget erschoepft ({spend:.2f}/{budget:.2f})",
        "en": "cloud {provider}: monthly budget exhausted ({spend:.2f}/{budget:.2f})"}),
    "cloud_budget_warning": ("warning", {
        "de": "Cloud {provider}: {percent:.0f} % des Monatsbudgets ({spend:.2f}/{budget:.2f})",
        "en": "cloud {provider}: {percent:.0f} % of the monthly budget ({spend:.2f}/{budget:.2f})"}),
}
CODES = tuple(CATALOGUE)


def make(code, node=None, subject=None, **params):
    """A record without message. `code` must be in CATALOGUE (a programming error otherwise, not an input)."""
    severity, _ = CATALOGUE[code]
    if node is not None:
        params["node"] = node
    return {"code": code, "severity": severity, "node": node, "subject": subject, "params": params}


def message(record, lang):
    """The record's text in `lang` (one of LANGUAGES)."""
    return CATALOGUE[record["code"]][1][lang].format(**record["params"])


def language(cfg=None):
    """The configured language of problem texts (router.problem_language)."""
    return getattr(cfg or state.CFG, "problem_language", "de")


def unservable_roles(now):
    """(unservable, limited): roles no online node could serve at all, and roles not servable now only because their
    nodes are busy (a game), which is intended and not a problem. Pure view, no breaker switching."""
    from . import scheduler   # late: scheduler imports nodes, which pull in much of the router
    unservable, limited = [], []
    for _exposed, role in state.CFG.roles.items():
        if scheduler.choose(role, role["tiers"], None, now, mutate=False):
            continue
        (limited if scheduler.role_possible_when_free(role) else unservable).append(role["name"])
    return unservable, limited


def compute(now=None, unservable=None, lang=None):
    """All active problems as records with message, in a fixed order (the order of the Home Assistant list since
    router 0.1). `unservable` lets ha_snapshot pass the role verdict it computed anyway; None computes it here."""
    now = now or time.time()
    lang = lang or language()
    online = [n for n in state.NODES.values() if n.state != "offline"]
    if unservable is None:
        unservable, _ = unservable_roles(now)
    out = []
    if not online:
        out.append(make("no_node_online"))
    for name in unservable:
        out.append(make("role_not_servable", subject=name, role=name))
    # Agent failure: node reachable, but the heartbeat agent, which has reported before, is silent.
    # Without an agent, there is no busy detection -> a game would no longer evict the large model.
    for n in online:
        if n.hb_ts and now - n.hb_ts > state.CFG.agent_missing_s:
            out.append(make("agent_heartbeat_missing", node=n.name, minutes=int((now - n.hb_ts) // 60)))
    # Router 0.9.2: a loaded model that Ollama had to place partly in system RAM runs there on the CPU, every request is many
    # times slower. Nothing fails, so without this fact it only shows as slow answers.
    if state.CFG.ram_spill_problem_gib > 0:
        for n in online:
            for m, g in sorted(n.ram_spill.items()):
                if g >= state.CFG.ram_spill_problem_gib:
                    out.append(make("ram_spill", node=n.name, subject=m, model=m, gib=g))
    # GPU guard (Agent >= 0.7.0): unavailable (limit not settable), deselected (operator decision: counts as a problem),
    # voltage/temperature/hardware warning. Only if the router should consider this node's status.
    if state.CFG.gpu_guard["enabled"]:
        for n in online:
            g = n.guard or {}
            if n.guard_policy and g.get("problem") and n.guard_state(now) is not None:
                reason = g.get("reason") or ", ".join(g.get("warnings") or []) or "?"
                out.append(make("gpu_guard", node=n.name, guard_state=g.get("state"), reason=reason))
    from . import agentupdate, cloud, ollamaupdate   # late: these modules import this one
    out += agentupdate.problems(now)    # failed or hanging agent updates
    out += ollamaupdate.problems(now)   # failed or hanging Ollama updates, persistently failing version check
    out += cloud.budget_problems()      # budget warning/exhaustion
    for r in out:
        r["message"] = message(r, lang)
    return out


def metric_rows(records):
    """Rows for the gauge skirnir_problem{code,severity,node,subject}: the number of active problems per label set
    (normally 1; equal label sets are summed so no series appears twice)."""
    counts = {}
    for r in records:
        key = (r["code"], r["severity"], r["node"] or "", r["subject"] or "")
        counts[key] = counts.get(key, 0) + 1
    return [({"code": c, "severity": s, "node": n, "subject": j}, v) for (c, s, n, j), v in sorted(counts.items())]


def view(records):
    """For GET /admin/problems and the UI."""
    return {"problems": records, "count": len(records),
            "by_severity": {s: sum(1 for r in records if r["severity"] == s) for s in SEVERITIES}}
