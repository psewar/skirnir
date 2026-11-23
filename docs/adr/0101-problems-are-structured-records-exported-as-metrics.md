---
status: proposed
date: 2026-11-23
decision-makers: operator
---

# Problems are structured records, exported as metrics

> Builds on [ADR-0022](0022-components-report-state-as-facts.md) (components report state as facts) and
> [ADR-0026](0026-english-names-for-interfaces-and-code.md) (English names for interfaces). Source: the portability
> analysis of 2026-11-22, which found that an operator without Home Assistant learns nothing about the router's problems.

## Context and Problem Statement

The router knows conditions an operator should act on although it keeps serving: no node online, a role no node can
serve, an agent that has gone silent, a model Ollama placed partly in system RAM, the GPU guard, failed or stalled agent
and Ollama updates, a failing Ollama version check, the cloud budget. Until router 0.34 `ha.ha_snapshot()` built these as
German sentences and the only consumer was the Home Assistant attribute `problems` over MQTT. `/metrics` had nothing.
Someone who runs Skirnir with Prometheus but without Home Assistant, or without MQTT at all, got no alert, and a program
that wanted to tell the cases apart had to match German text.

The maintainer's Home Assistant automations match those sentences, so they must keep arriving unchanged.

## Decision Drivers

* An alert path that does not depend on MQTT or Home Assistant.
* Cases are told apart by a fixed value, not by parsing text (AGENTS.md, explicit control flow).
* Bounded label cardinality in `/metrics`: no free text, no unbounded identities as labels.
* Existing Home Assistant entities, attributes and texts stay exactly as they are by default.
* English identifiers on the wire (ADR-0026); German stays a translation.

## Considered Options

* **Records with a fixed code computed in one module; a message catalogue (German, English); HA, `/metrics`, an admin
  endpoint and the UI read from it.**
* Keep the sentences and add a Prometheus metric per sentence. Rejected: the sentence carries values (minutes, GiB,
  percent, an agent's error message), so every change of value would be a new series, and the codes would still have to
  be parsed out of German text.
* Switch the HA attribute to the structured records. Rejected: it breaks existing automations that match the sentences,
  for no gain in Home Assistant itself.
* Push alerts to a webhook or Alertmanager directly. Not now: it adds a delivery path with retries and credentials, while
  a gauge lets any Prometheus-compatible system alert with its own routing. A later sink can read the same records.

## Decision Outcome

Chosen option: records with a fixed code.

* `router/skirnir_router/problems.py` computes the list, independent of MQTT. A record is
  `{code, severity, node, subject, params, message}`. The producers that live elsewhere (agent and Ollama updates, cloud
  budget) return records built with `problems.make`, no longer text.
* **Codes** (snake_case English, fixed in `CATALOGUE`): `no_node_online`, `role_not_servable`, `agent_heartbeat_missing`,
  `ram_spill`, `gpu_guard`, `agent_update_failed`, `agent_update_stalled`, `ollama_update_failed`,
  `ollama_update_stalled`, `ollama_version_check_failing`, `cloud_budget_exhausted`, `cloud_budget_warning`.
* **Severity** is fixed per code and says what happens to requests, not how bad the hardware looks: `critical` when
  requests fail or a configured tier drops out now (`no_node_online`, `role_not_servable`, `cloud_budget_exhausted`),
  `warning` when the router still serves but slower, without a safety function or without fresh facts (all others). The
  GPU guard is `warning` because the agent acts on its own hardware warnings; the router only loses its view.
* **Identities**: `node` is the node the problem is about, `subject` the second identity where a code can occur several
  times: the role, the cloud provider, the model. They come from the configuration, the agent registry or the models a
  node reports (already labels of `skirnir_model_loaded_gib`). Free text from elsewhere (an agent's update message, the
  version check error) stays in `params`.
* **Texts** come from the catalogue in German and English. The new key `router.problem_language` (`de` default, `en`;
  UI-editable) selects the language for the HA attribute and for `GET /admin/problems`. The German templates are byte for
  byte the sentences of router 0.34. Templates are filled once with `str.format`; a reported name cannot inject a
  placeholder.
* **`/metrics`**: `skirnir_problem{code,severity,node,subject}` with the number of active problems with that label set
  (normally 1; equal label sets are summed so no series appears twice) and `skirnir_problems_active`. Empty `node` or
  `subject` means none. Label values now also escape a line feed.
* **`GET /admin/problems[?lang=de|en]`** returns `{problems, count, by_severity, language}` behind the control port's
  Basic Auth like every `/admin` GET; an unknown `lang` is a 400.
* **Web UI**: the overview lists the problems (severity, code, text in the UI language), built with `textContent` only
  ([ADR-0065](0065-browser-headers-and-handler-escaping-for-the-control-ui.md)).

### Consequences

* Good: Prometheus/Alertmanager (or any scraper) can alert on `skirnir_problem` or `skirnir_problems_active > 0` without
  Home Assistant or MQTT.
* Good: a program tells cases apart by `code`; adding a problem is one catalogue entry and one producer.
* Good: Home Assistant sees no change unless the operator sets `problem_language: en`.
* Bad: a new code needs a German and an English text; the self-test refuses a code without both.
* Bad: `/metrics` and `/admin/problems` compute the problems on every request (the same pure view HA computes every
  interval: one scheduler pass over the roles, no side effects). Acceptable at scrape intervals.
* Neutral: texts that agents send (GPU guard reason, update messages) stay in the agent's language; translating them is a
  matter for the agent.

### Confirmation

`test/selftest_problems.py` raises every code from its real condition and compares the German text with the sentence of
router 0.34; checks the English texts, the HA view in both languages, the gauge lines and their label escaping, the summing
of equal label sets, `GET /admin/problems` through the real control app (401 without or with wrong credentials, 400 for an
unknown language, records with credentials) and that an unknown `problem_language` stops the configuration. The existing
end-to-end checks on the HA problem texts (`test/run_tests.py`) keep passing unchanged.
