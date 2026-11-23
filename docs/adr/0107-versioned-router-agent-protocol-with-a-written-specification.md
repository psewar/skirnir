---
status: proposed
date: 2026-11-23
decision-makers: operator
---

# Versioned router–agent protocol with a written specification

> Builds on [ADR-0008](0008-outbound-tunnel-from-the-agent.md) (outbound tunnel from the agent),
> [ADR-0009](0009-agent-identity-by-key-and-operator-approval.md) (agent identity by key and operator approval) and
> [ADR-0022](0022-components-report-state-as-facts.md) (components report state as facts).

## Context and Problem Statement

The router and the node agent talk over one WebSocket: a signed login, binary frames for heartbeats, upstream reports,
power notices and multiplexed request streams, and JSON control messages. The protocol has grown over dozens of agent
releases by adding optional fields and frames. It was written down only in comments of `agent/tunnel.go` and
`router/skirnir_router/tunnel.py` and implicitly in `test/fake_agent.py`; the heartbeat is an untyped map, and optional
features are announced as a growing set of boolean facts.

Skirnir is opening up for other setups. A node agent for another platform (for example a Mac, or a server that only runs
an OpenAI-compatible backend) currently has to be reverse-engineered from three code bases, and nothing tells a
contributor whether a change to one side breaks the other. Three implementations exist (router, Go agent, test fake
agent) and nothing checks that they agree.

## Decision Drivers

* Someone who did not write the code can implement an agent from documents alone.
* Specification and implementations cannot drift silently: a change on one side fails a test until the other sides and
  the documentation follow.
* No behaviour change and no version bump for running routers and agents; existing agents stay compatible.
* No new third-party dependency (ADR-0074: hash-locked installs; the CI lock has no JSON Schema package).
* Explicit control flow and facts (ADR-0022, ADR-0023): "required" and "optional" must describe what the receiver really
  does.

## Considered Options

* **Specify the existing protocol as version 1: prose in `docs/protocol.md`, one JSON Schema per message and an index of
  frame codes, allowlist and limits in `docs/protocol/`, and conformance tests on both implementations.**
* Introduce a protocol version field and a capability list now, together with the specification. Rejected for this step:
  it changes router and agent code and the wire, needs a version negotiation and a migration of the boolean facts, and
  would have collided with several concurrent changes to the same files. Specifying the existing protocol first gives the
  later change a precise baseline.
* Replace the hand-written protocol with an IDL (protobuf, Cap'n Proto) or a generated client. Rejected: it changes the
  wire format of a working system, adds code generation and dependencies to both builds, and the JSON payloads are
  deliberately open for unknown fields.
* Documentation only, without tests. Rejected: it would be wrong after the next agent release, which is the problem the
  comments already have.

## Decision Outcome

Chosen option: specify the existing protocol as version 1, with tests that bind it to the code.

* `docs/protocol.md` (and `docs/protocol.de.md`) describes transport, login, frames, request streams, heartbeat, upstream
  and power reports, control messages, compatibility rules and the security properties.
* `docs/protocol/*.schema.json` describe each message in a subset of JSON Schema 2020-12. `required` means the receiver
  refuses or drops the message without the field; everything else is optional with a documented meaning when absent.
  Each field names the agent or router version it appeared in (`x-since-agent`, `x-since-router`; `baseline` for fields
  older than this repository's history). `x-legacy` marks fields the router reads but the current agent does not send,
  `x-ignored-by-receiver` informational fields, `x-open` objects defined elsewhere (Ollama's model entries).
* `docs/protocol/index.json` holds the frame codes, the text message types and their schemas, the agent's passthrough
  allowlist and the size and time limits.
* `test/protocol_schema.py` is a dependency-free validator for exactly that subset; it refuses schema keywords it does not
  implement, so a schema cannot promise a check that never runs. It also serves third-party agent authors as a CLI.
* `test/selftest_protocol.py` (CI) checks the schemas and their examples; compares frame codes and limits with the router
  and the fake agent; checks statically that every control message the router builds and every field it reads from an
  agent is documented; checks in the router that a message without a required field is refused or dropped and that the
  examples are accepted; and runs the real router against `test/fake_agent.py` through a relay that validates every
  message in both directions strictly.
* `agent/protocol_conformance_test.go` (CI, Linux and Windows) compares the Go agent's message types (facts, OLLAMA,
  POWER, the heartbeat's keys from `Heartbeat.payload` and its nested blocks) with the schemas in both directions,
  decodes every documented router message and example into the agent's types, compares frame codes and the passthrough
  allowlist with the index, and runs the real login against a fake router (signed, conforming hello; nonce under
  16 bytes refused).
* Router and agent code are unchanged; there is no new version and no tag.

### Planned next step (not decided here)

Version 1 has no version number on the wire. The follow-up, to be decided in its own ADR with the code change, is a field
`proto` (integer, absent = 1) in hello and challenge and a list `capabilities[]` in the facts that replaces new boolean
feature facts (existing ones stay as aliases). The schemas and tests of this ADR are the baseline that change is measured
against.

### Consequences

* Good: an agent can be written from `docs/protocol.md` and the schemas, and checked with `test/protocol_schema.py`.
* Good: a field added on one side without the other side and the documentation fails CI, in Python and in Go.
* Good: the "required" claims are tested against the router's behaviour, not only written down.
* Bad: every protocol change now touches the schemas, both language versions of the page and possibly the test tables
  (the Go heartbeat test maps each heartbeat key to its Go type). This is the intended cost.
* Bad: the heartbeat test reads `Heartbeat.payload` with the Go parser, because the payload is an untyped map; a
  refactoring of that function has to keep the test working (it fails loudly if it finds no keys).
* Bad: concurrent pull requests that change the wire (for example a heartbeat without GPU fields) must update the
  schemas when they are rebased onto this one.

### Confirmation

`python test/selftest_protocol.py` and `go test ./...` in `agent/` run in CI. Mutation checks during review: an extra
field in the fake agent's heartbeat, an undocumented field read or sent by the router, an undocumented Facts field, a
renamed or retyped heartbeat property, an undocumented control message field and a changed allowlist each fail the
tests.

## More Information

Source: analysis of portability work for other setups, phase 1.2 (2026-11-22). Documentation:
[docs/protocol.md](../protocol.md).
