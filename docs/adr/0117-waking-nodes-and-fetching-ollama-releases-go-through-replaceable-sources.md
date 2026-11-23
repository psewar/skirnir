---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# Waking nodes and fetching Ollama releases go through replaceable sources

> Part of the portability roadmap, phase 3 ([design/portability.md](../../design/portability.md)). Keeps
> [ADR-0021](0021-ollama-as-agent-child-updated-through-the-router.md) (Ollama updated through the router; the router
> chooses only the version, never the code) and the downgrade refusal of
> [ADR-0073](0073-close-router-paths-to-host-privileges.md). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

**Waking.** The router wakes an offline node only by Wake-on-LAN: `send_magic_packet` in
`router/skirnir_router/wol.py` (lines 11-18) sends a UDP broadcast to port 9 on each address in
`wol.broadcast_addresses` (`router/skirnir_router/config.py`, line 558), and `wake` (from line 21) sends it, polls the
node and retries; the proxy calls it before a request to an offline node (`router/skirnir_router/proxy.py`, line 325).
Machines that are switched by a smart plug, a Home Assistant or ioBroker script, IPMI or a cloud API, or that sit in
another broadcast domain, cannot be woken.

**Ollama releases.** The router's version check is GitHub-shaped: `release_url` defaults to the GitHub API's latest
release of `ollama/ollama` (`router/config.example.yaml`, line 327), and `_fetch_release` in
`router/skirnir_router/ollamaupdate.py` (from line 99) reads `tag_name`, `assets` and `browser_download_url` and the
release's `sha256sum.txt`, with asset names fixed per platform (`ASSETS`, lines 31-32). The agent downloads the archive
itself from its own source, `ollama_update.source` with the default GitHub download prefix and the layout
`<source>v<version>/<file>` plus `sha256sum.txt` (`agent/ollamaupdate.go`, lines 36 and 42), and compares the hash with
that file. So the router can choose the version but not the code (see the header of `ollamaupdate.py`). A mirror, an
offline repository or a non-GitHub source does not fit the router side.

## Decision Drivers

* Wake any machine the operator can switch, not only WOL-capable ones on the same segment.
* Ollama releases from a mirror or an internal source, for offline or restricted networks.
* The update trust model stays: the router selects a version, the agent fetches and verifies from its node-local source.
* No arbitrary command execution on the router (ADR-0118).

## Considered Options

* A `Waker` interface (`wol`, `http_webhook`, `ipmi`) per node, and a `ReleaseSource` with a neutral release document.
* Keep WOL only and suggest the operator's home automation wakes machines itself.
* An "exec a command" waker.

## Decision Outcome

Chosen option: "`Waker` per node and a `ReleaseSource`", because both cover the common setups with fixed, reviewable
adapters and leave the update trust model as it is.

* `Waker` with `wake(node) -> sent | Problem`; the wait-and-poll loop stays in one place around it. Implementations:
  `wol` (today, default), `http_webhook` (a configured URL with an optional secret from the environment, for Home
  Assistant scripts, smart plugs, ioBroker; egress explicit), `ipmi` (power on via the BMC, credentials by environment
  reference). Selected per node in its policy or configuration.
* `ReleaseSource` for the router's version check: `github` (today, default) or `document`, a URL that returns
  `{version, files: {"<os>/<arch>": {url, sha256, size}}}`. What it yields is a version and expected hashes; the order to
  the agent stays as today.
* The agent keeps fetching from its node-local `ollama_update.source` and verifying against that source's
  `sha256sum.txt`; a router-side source never supplies code to the agent.

### Consequences

* Good, because nodes behind smart plugs, BMCs or another subnet can be woken on demand.
* Good, because installations without GitHub access can follow an internal mirror.
* Bad, because a webhook or IPMI waker is a new outbound destination with credentials; it needs the same egress and
  secret handling as other outbound calls (B9).
* Bad, because a waker that reports `sent` does not mean the machine is coming up; the poll loop stays the only judge.

### Confirmation

Self-tests: the `wol` waker sends the same packet as today; `http_webhook` against a local fake receiver, including a
timeout that does not block other requests; an unknown waker kind is refused at configuration load. A test feeds the
`document` release source and checks the resulting order matches what the GitHub source produces for the same release,
and a test confirms the agent still refuses an archive whose hash does not match its own source.
