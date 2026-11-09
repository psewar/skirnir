---
status: accepted
date: 2026-11-10
decision-makers: operator
---

# Close the paths from a compromised router to host privileges

> Source: TM-14 review on 2026-11-09 of what the router and the agent can reach with their rights.

## Context and Problem Statement

ADR-0044 runs the router as root without capabilities, with the file system read-only except `/etc/skirnir-router` and
its state and log folders. Three paths still led from a compromised router to more than it should have:

1. **Root helper sources a router-writable file.** `render-env.sh` (secrets timer) and `render-monitoring.sh` source
   `/etc/skirnir-router/render-env.conf` as full root. The router is uid 0, so even without `CAP_DAC_OVERRIDE` the owner
   bits let it rewrite that root-owned file; the next timer run executes its content with all capabilities. The same holds
   for `SECRET_STORE_AUTH_FILE` if it pointed there, and for the certificate folder the ACME client writes as root.
2. **The agent forwards any path to Ollama.** `Tunnel.handle` sent every request without a configured route to the
   upstream. The router refuses model management for its clients, but the agent did not, so a compromised router could
   call `/api/pull`, `/api/create`, `/api/delete` or `/api/blobs` on an Ollama that runs with the agent's service rights
   (LocalSystem on Windows) - historically a source of path-traversal file writes.
3. **The router picks the Ollama version.** The update order is checked against the release's `sha256sum.txt`, but any
   version was accepted, so a compromised router could pin an old release with known flaws.

## Decision Drivers

* Each check where the privilege is, not in the component that might be compromised.
* No change for normal operation: the router never sends model management, never needs the secret-store access file and
  never writes certificates.
* Existing installations migrate in the same deploy, without a manual step.

## Considered Options

* Move and guard the access file; an allow-list in the agent; a downgrade refusal for Ollama orders
* Run the router under its own user first (removes path 1 more broadly)
* Parse `render-env.conf` as key=value instead of sourcing it

## Decision Outcome

Chosen option: "move and guard; allow-list; downgrade refusal", because each closes one path now with a small change. A
dedicated router user is still planned (TM-14), but needs ownership changes across install, deploy and certificate
renewal and a staging run. Parsing the access file would break existing files that map variables from another file.

* The access file lives in `/etc/skirnir-router-secrets/` (0700 root, file 0600). `deploy.py` and `install.sh` create the
  folder and move an old `/etc/skirnir-router/render-env.conf` there. Both renderers refuse - and render nothing, exit 0 -
  when the file or `SECRET_STORE_AUTH_FILE` resolves into a router-writable folder, is not owned by root, or the file or
  its folder is writable by group or others; they never read the old place.
* The router unit gets `ReadOnlyPaths=-/etc/skirnir-router/certs` and `InaccessiblePaths=-/etc/skirnir-router-secrets`.
  The secret and certificate helper units get `NoNewPrivileges`, `PrivateTmp`, `ProtectHome` and the kernel protections
  that do not affect writing `secrets.env` or restarting the router.
* Agent 0.31.0 forwards without a configured route only `POST /api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings`,
  `/api/show`, `GET /api/tags`, `/api/ps`, `/api/version`, `POST /v1/chat/completions`, `/v1/embeddings` and
  `GET /v1/models`, compared exactly (no prefix, no case folding). Anything else gets 403 `path_not_allowed_on_node`
  without an upstream call, and a log line with method and path.
* Agent 0.31.0 refuses an Ollama order older than the installed version (from `/api/version`, else the binary) or not a
  plain release, unless `ollama_update.allow_downgrade: true`; the same rule as the agent's own update (ADR-0054).

### Consequences

* Good, because a compromised router no longer reaches full root through the timer, nor Ollama's model management, nor an
  older Ollama.
* Good, because the agent's list and the router's paths are checked against each other in CI.
* Bad, because a client that used model management through the router never could; one that relied on another Ollama path
  through the tunnel (none known) now gets 403 and needs a new agent version.
* Bad, because a deliberate Ollama rollback needs a configuration change on the node.
* Neutral, because the router still runs as root; this removes known escalation paths, not the root identity.

### Confirmation

`test/selftest_privilege_paths.py`: both renderers source a root-only file and refuse one in a router-writable folder, a
symlink into it, a group-writable file, a world-writable folder and such an auth file; the units carry the directives;
`deploy.py` and the renderers agree on a path outside `ReadWritePaths`; the agent's list covers every path the router
sends and none it forbids. `agent/passthrough_test.go`: every listed path reaches the upstream, model management, blobs,
other methods and near misses get 403 without an upstream call. `agent/ollamaupdate_test.go`: older and pre-release
orders are refused before any download; `allow_downgrade` lets them pass.
