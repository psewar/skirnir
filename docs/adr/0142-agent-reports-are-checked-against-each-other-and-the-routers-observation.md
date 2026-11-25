---
status: proposed
date: 2026-11-25
decision-makers: maintainer (asked for by kay, proposed by Claude)
---

# Agent reports are checked against each other and the router's observation

> Threat: TM-07 (next action "cross-checks of plausible but false reports"). Builds on
> [ADR-0137](0137-the-router-uses-only-plausible-gpu-numbers-from-an-agent.md) (only plausible GPU numbers are used).

## Context and Problem Statement

Since ADR-0137 the router drops GPU numbers no card can have. A compromised or broken approved agent can still send
numbers that are each plausible but false together: plenty of free VRAM while it reports large models loaded (which
attracts requests it cannot serve or should not see), or an idle GPU while it is producing the router's own output (which
hides load). Most numbers the router could compare against come from the same agent (Ollama's rates and model lists
travel through it), so they prove nothing on their own. Two comparisons do not depend on trusting one report: the agent's
reports against each other, and the agent's reports against what the router itself sees on the wire.

## Decision Drivers

* Detect contradictions without trusting any single report of the agent.
* No false alarms from ordinary lag (load and unload tails, sampling) or from a second GPU the agent does not report.
* An operator sees it where problems are shown and can act (revoke the node); routing is not changed automatically.
* Bounded noise: one audit line per contradiction, not per heartbeat.

## Considered Options

* **Two checks, each counted only after three consecutive heartbeats: free VRAM against the agent's own loaded models,
  and GPU utilisation against the router's own observation of output from the node; a fact, a problem and one audit line;
  routing unchanged.**
* Compare Ollama's reported generation rate with the node's benchmark. Rejected: both come from the agent.
* Act automatically (treat the node as busy or offline). Rejected for now: a false positive would take a working node
  out of service; the operator has the evidence and the revoke button.

## Decision Outcome

Chosen option: "two cross-checks as a fact and a problem", because both use a source the agent does not control alone and
neither changes routing on a guess.

* `free_vram`: the reported free VRAM exceeds what the reported card can hold next to the models the same agent reports
  loaded (`total - loaded + 1 GiB`). Only when the loaded models fit the reported card, so a second GPU the agent does not
  report (it reports the first) does not count.
* `gpu_idle`: the reported GPU utilisation is at most 2 % while the router has received generated output from this node
  within the last two heartbeat intervals (`Node.last_output_at`, stamped by the relay's stream loop, the router's own
  observation). Not for a node that reports no GPU.
* `poll.note_report_contradictions` runs after every heartbeat: a contradiction present in three consecutive heartbeats
  becomes `Node.report_mismatch` (node view), the problem `agent_free_vram_mismatch` or `agent_idle_while_generating`
  (UI, Home Assistant, metric `skirnir_problem`), a warning in the log and one audit line `agent_report_mismatch`; it
  clears with the first heartbeat that agrees again.

### Consequences

* Good, because the two lies that most change where requests go are visible as problems with the numbers that contradict
  each other.
* Good, because nothing is taken out of service on a guess.
* Bad, because a consistent liar (free VRAM that fits its own fake loaded list, a fake utilisation while generating) still
  passes; these checks raise the bar, they do not prove honesty. Fingerprint checks and revocation stay the controls.
* Bad, because a node whose Ollama works on a second GPU the agent does not report raises `agent_idle_while_generating`;
  the message then correctly says the reports do not describe the GPU doing the work.

### Confirmation

`test/selftest_report_crosscheck.py` (in CI): free VRAM above the room next to the loaded models is found, within the slack
not, loaded models larger than the card (second GPU) and none loaded not; an idle GPU while the router received output
seconds ago is found, a busy GPU, no recent output and a node without GPU not; a contradiction becomes a fact only on the
third consecutive heartbeat, writes one warning and one audit line, shows as a problem and in the node view, leaves the
node's state alone, and clears with an agreeing heartbeat; both kinds raise their own problem with English text; the relay
stamps the node in its stream loop. `test/selftest_problems.py` raises both new codes in its scenario.
