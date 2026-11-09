---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Standalone agent key and release tool

> Source: documentation review of the first installation; TM-13 in the threat model. Builds on ADR-0058 and ADR-0066.

## Context and Problem Statement

Signed agent updates need an operator key and a signed manifest next to the binaries on the router. Both were only
reachable through `deploy.py`, which loads the maintainer's deploy environment (an ops folder with `deploy.env`, a secret
store module, an LXC host reached over SSH) and imports paramiko on start. An operator without that environment could
neither create a key, rotate it, replace a lost one, nor publish an agent release, although none of these steps needs SSH.

## Decision Drivers

* Key and release steps must work offline with only Python and `cryptography`.
* One implementation of the manifest format the agent verifies, used by every path.
* The key stays off the router; the operator copies only public results there.

## Considered Options

* `deploy/agent_keys.py` as a command line tool (`--keys <dir>`: state, create, next, use-next, replace-lost,
  sign-release) and `deploy.py` using its functions
* Make `deploy.py` load its environment lazily so the key options work without it
* Sign releases in CI

## Decision Outcome

Chosen option: "Standalone `agent_keys.py`", because it separates what every operator needs from the maintainer's deploy
transport and keeps one manifest implementation. A lazily loading `deploy.py` would still ship SSH code to people who do
not use it; signing in CI would put the signing key into CI.

* `python deploy/agent_keys.py --keys <dir> sign-release <dist> --version <v> [--out <dir>]` takes the build names
  (`skirnir-agent.exe`, `skirnir-agent-linux-amd64`) or the release asset names, writes the files under their release names
  and `manifest.json` into `<dist>/publish` (or `--out`) and prints where to copy them (`/etc/skirnir-router/agent/`) and
  the public key the nodes need. It refuses without a key, with a malformed version, without binaries, or over an
  existing manifest.
* `deploy.py --agent` signs with the same `signed_manifest`; the list of agent binaries is shared.

### Consequences

* Good, because an operator can run signed agent updates without the maintainer's tooling.
* Good, because the manifest format has one implementation and a test that verifies it like the agent.
* Bad, because the version must be passed explicitly; the tool does not run the binaries to ask them.
* Neutral, because copying the files to the router stays a manual (or the operator's own) step.

### Confirmation

`test/selftest_key_rotation.py`: the CLI without a deploy environment creates a key and reports its state;
`sign-release` refuses without version, with a malformed version, without binaries and over a manifest; it writes the
release-named files and a manifest whose signature verifies with the public key, while a changed manifest does not;
hashes and sizes match; `deploy.py` uses the shared manifest code.
