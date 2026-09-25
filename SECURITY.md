# Security

Skirnir sits between your clients and your GPU machines and, optionally, cloud providers. It is a homelab project with one
developer and no stability guarantees, but security reports are taken seriously.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** on this repository (Security tab → "Report a vulnerability") rather
than a public issue. Include the version (`/api/version` reports `router<x.y.z>`, the agent prints `skirnir-agent version`),
what you observed, and how to reproduce it. You should get a first reply within a week.

## Supported versions

Only the latest release is supported. Fixes land on `main` and are tagged; there are no maintenance branches.

## What the trust model assumes

- The **inference port** (11434) is reachable by your clients; client identities (Bearer, Basic, source IP) and rate limits are
  the boundary. The **control port** (11435) carries the web UI and `/admin/*` behind Basic Auth and TLS; if you expose it
  (`public_url`), a failed login costs a hashed password check in a thread and five failures per minute lock the source address.
- **Agents** authenticate with an Ed25519 key over an outbound WebSocket; nothing listens on the GPU node for the router. A new
  key is *pending* until an operator approves it in the UI.
- **Ollama updates** are fetched by the agent itself from a fixed source (`ollama_update.source`, default GitHub releases of
  ollama/ollama) and checked against that release's `sha256sum.txt`; the router only names the version.
- **Agent updates** are signed with an operator key that never lives on the router. A compromised router can misroute
  inference but cannot push code to the nodes.
- The **agent's local health port** (127.0.0.1:10398) is for diagnostics; `POST /restart-child` needs the token in
  `control.token` next to the configuration, and relayed GPU-Z sensor values never influence the power limit.
- Cloud tiers are opt-in per role and data class; nothing leaves the network by default, and a credential scan can block
  requests that look like they carry secrets.

Things that are deliberately *not* in scope: protecting against a malicious operator, or against someone with administrator
rights on a GPU node.
