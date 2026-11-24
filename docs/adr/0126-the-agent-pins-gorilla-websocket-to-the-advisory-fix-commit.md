---
status: accepted
date: 2026-11-24
decision-makers: maintainer (proposed by Claude)
---

# The agent pins gorilla/websocket to the advisory's fix commit until a release contains it

> Keeps [ADR-0045](0045-dependency-scanning-in-ci.md) (govulncheck gates every pull request) and
> [ADR-0055](0055-pin-third-party-references-immutably.md) (immutable references; Go modules are fixed by `go.sum`).

## Context and Problem Statement

The node agent uses `github.com/gorilla/websocket` as the client of its tunnel to the router (`agent/tunnel.go`, `ws://`
or `wss://` depending on the router URL), and `github.com/eclipse/paho.mqtt.golang` uses it for MQTT over WebSockets.
The agent required v1.5.3, the newest tag (2024-06-14).

Advisory GO-2026-6278 (GHSA-w67g-5rqw-f597): v1.5.3 generates the client's frame mask keys with `math/rand`
(`rand.Uint32()` in `newMaskKey`). The Go vulnerability database lists versions before v1.5.1 and from v1.5.3 up to the
untagged commit `d67f418` ("Use crypto/rand for mask key") as affected. v1.5.3 reset the package to an older commit
(`931041c`, per its release notes), so it lacks the fix that v1.5.1 had; the fix lives only on upstream `main`, and no
release since v1.5.3 exists. govulncheck therefore fails the required `go-security` check on every pull request and on
`main`.

Masking exists so that a client's frames cannot be used to poison intermediaries on unencrypted connections; over
`wss://` a predictable mask key matters little. The gate is still right to fail: the agent may use `ws://`, and paho may
use WebSockets.

v1.5.1 and v1.5.2 are not affected but cannot be selected: paho v1.5.1 and v1.5.2 require v1.5.3, and Go's minimal
version selection takes the highest required version (v1.5.2 is also retracted upstream).

## Decision Drivers

* The required vulnerability gate stays as strict as it is: no exclusion, no accepted finding.
* The smallest change to the code that carries the tunnel.
* Reproducible: an exact version with its `go.sum` hashes, verified against the Go checksum database.
* Back to a tagged release as soon as upstream publishes one that contains the fix.

## Considered Options

* **Pin the commit the advisory names as the fix, `v1.5.4-0.20240701034025-d67f41855da4`.**
* Pin the newest upstream commit (`v1.5.4-0.20250319132907-e064f32e3674`). Rejected for now: eight more months of
  upstream changes that no release contains and that the agent does not need for the fix.
* Accept the finding with an expiry and exclude it from govulncheck. Rejected: it weakens a required gate for a defect
  that a version change removes.
* Replace the library (for example `github.com/coder/websocket`). Rejected: a larger change to the tunnel, and paho would
  still pull gorilla/websocket.
* Go back to v1.5.1 or v1.5.2. Not possible (minimal version selection, see above).

## Decision Outcome

Chosen option (accepted by the maintainer 2026-11-24): pin the fix commit `d67f418`, agent 0.36.1.

The fix commit is 14 commits ahead of v1.5.3. For the agent's client the differences are: mask keys from
`crypto/rand`; the dial code refactored to pass the context (for a `Dialer` without `NetDial`/`NetDialContext`, as the
agent's, still `net.Dialer.DialContext`); `ioutil` replaced by `io`; the proxy code now taken from
`golang.org/x/net/proxy` (only used with a `Proxy`, which the agent does not set; `x/net` is already in the build at a
newer version through paho). The other commits change the server side, which the agent does not use, and tests.

### Consequences

* Good, because the gate stays strict and green, and the agent's mask keys come from `crypto/rand`.
* Good, because the change is two lines in `go.mod` and `go.sum`.
* Bad, because the agent depends on an untagged upstream commit with changes no release contains.
* Bad, because someone has to move to a tag when upstream publishes one; Dependabot proposes it, since a tag v1.5.4 or
  later sorts above the pseudo-version.

### Confirmation

govulncheck (`.github/workflows/security.yml`) reports no vulnerability for the agent. `go test ./...` on Linux and
Windows passes, including the tests that open real WebSocket connections through the tunnel (protocol conformance,
router TLS, routes, power). When a tagged release contains the fix, a pull request replaces the pseudo-version with it
and this ADR becomes `superseded`.
