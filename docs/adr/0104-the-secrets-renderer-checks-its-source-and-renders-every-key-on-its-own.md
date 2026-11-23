---
status: proposed
date: 2026-11-23
decision-makers: operator
---

# The secrets renderer checks its source and renders every key on its own

> Source: opening Skirnir for other setups (portability work, phase 0). Proposed for the maintainer's decision.

## Context and Problem Statement

`router/render-env.sh` renders `/etc/skirnir-router/secrets.env`, the router unit's `EnvironmentFile`. Up to router 0.34.1
it fetched `MQTT_PASSWORD` first and, if that value was missing, wrote nothing at all and printed "secret store not
reachable?". Two problems follow:

* A router without Home Assistant (no MQTT) never got a `secrets.env`: no cloud key, no session-store DSN, no memory
  secrets, although MQTT is optional in the router.
* The script inferred a state (store unreachable) from a missing value. ADR-0022 requires state to be reported as a fact;
  a missing secret and an unreachable store are different conditions with different next steps.

The renderer was also tied to one kind of secret store (a universal-auth API). Operators without such a store had only a
hand-maintained `secrets.env`, which works but is undocumented as a path and gets none of the renderer's name and value
checks. Separately, `install.sh` always installed the MQTT library (`python3-paho-mqtt`), although the router checks for it
with `find_spec` and runs without it when no `mqtt:` section is configured.

## Decision Drivers

* No optional integration (MQTT) may gate unrelated secrets.
* Reachability is a fact of its own, reported before any value is fetched (ADR-0022).
* Keep every safety property of ADR-0073, ADR-0080 and ADR-0086: the access file is sourced only from a root-only folder
  outside the router's paths, names are restricted, values with a line break are refused, the file is written through a
  temporary file in the same folder and renamed, group `skirnir-router` and mode 0640 (0600 without the account).
* A transient fault must not drop a secret that was rendered before.
* Existing `render-env.conf` files keep working unchanged; the default installation still installs the MQTT library.

## Considered Options

* Explicit steps (source ready, then every name on its own, then render what is present) with a selectable source:
  `store` (default) and `file`
* Keep the MQTT gate and only change its message
* Render what is present even when one fetch failed
* Add a client for a further store (e.g. Vault) now

## Decision Outcome

Chosen option: "explicit steps with a selectable source", because it removes the gate, reports each condition as what it
is and opens a no-store path without weakening any check.

* **Step 1, source ready.** `store`: the HTTP status of the login is the fact - no answer (`not reachable`), 400/401/403
  (`refused the login`), another status, or a token (`ready`). `file`: `SECRET_FILE` exists, passes the same root-ownership
  and not-router-writable checks as the access file, is a regular file and is readable by root only (mode `& 077 == 0`).
  A source that is not ready keeps the existing `secrets.env`.
* **Step 2, every name on its own.** Each built-in name and each valid `EXTRA_SECRET_KEYS` name is fetched separately and
  is `present`, `missing` (store: HTTP 404; file: no line) or `failed` (no answer, another status, a 200 without a string
  value). `MQTT_PASSWORD` is one optional built-in name like the others; a value with a line break is skipped by name for
  every name, including `MQTT_PASSWORD` (before, that one value stopped all rendering).
* **Step 3, render.** If any fetch failed, the existing file is kept as a whole, so a transient fault drops nothing. If the
  source holds none of the names, the existing file is kept too (more likely a wrong `PROJECT_ID`, `ENVIRONMENT` or
  `SECRET_FILE` than an intent to empty it). Otherwise every present name is written, with the unchanged temporary-file,
  rename, group and mode handling.
* **The last line is the state**: `STATE: written | unchanged | kept | not-configured | refused`, like `install.sh`.
* **`file` source**: `KEY=value` per line, the value raw to the end of the line (the format of `secrets.env`), comments and
  blank lines skipped, read line by line and never sourced. Only built-in and `EXTRA_SECRET_KEYS` names are copied; other
  names, invalid lines and names given twice are reported by line number or name and skipped.
* **Without a renderer**: no `render-env.conf` means nothing is rendered (`not-configured`); `secrets.env` can be
  maintained by hand. systemd `LoadCredential=` can feed the values the router reads from a file
  (`mqtt.password_file`, a cloud provider's `secrets_file`); the session-store and memory variables come only from the
  environment and need `secrets.env` or another `EnvironmentFile`.
* **The bearer token goes to curl through stdin** (`-H @-`), like the login body, so neither appears in the process list.
* **`install.sh --without-mqtt`** leaves out `python3-paho-mqtt` and tells `pydeps.py` (`--without paho-mqtt`) not to install
  `paho-mqtt` from the lock; it is refused while `config.yaml` names an MQTT broker (`mqtt.host`). `pydeps.py` refuses a
  `--without` name that is no requirement, so a typo never installs what it meant to leave out. Without the option nothing
  changes.

Keeping the gate with a better message would still leave every MQTT-less router without secrets. Rendering the present
names after a failed fetch would replace a good file with one that lacks a key because of a network fault. A Vault (or
other store) client is a possible next step behind the same `fetch` interface, but none is needed to remove the gate and
writing one without a user would be speculative.

### Consequences

* Good, because a router without MQTT gets its cloud keys, DSN and memory secrets from the renderer.
* Good, because "not reachable", "login refused", "missing" and "fetch failed" are separate, named outcomes, and the state
  line can be read by tools.
* Good, because operators without a secret store get the renderer's name and value checks with a root-only file.
* Bad, because a store that answers a missing secret with something other than 404 now keeps the file (fetch failed)
  instead of treating the name as missing; the log names the name and the status.
* Bad, because a `MQTT_PASSWORD` with a line break is now dropped from a new `secrets.env` instead of keeping the whole old
  file; the router then has no MQTT password until the value is fixed (the log names it).
* Bad, because removing the last secret from the source does not empty `secrets.env` (the file is kept); to drop it on
  purpose, delete the file by hand. Removing one of several secrets works as before (the name is missing and not written).
* Bad, because while one name keeps failing to fetch, a rotated value of another name does not reach the file either; the
  log names the failing name on every run.
* Neutral, because `monitoring/render-monitoring.sh` never had the gate (it fetches each of its two names on its own and
  keeps the old file for a name it does not get); it is unchanged.

### Confirmation

`test/selftest_render_env.py` runs `render-env.sh` in CI against a fake store and a root-only file: without
`MQTT_PASSWORD` every other name is rendered; a store on a closed port, a refused login, one name answering 500 and a store
with none of the names each keep the existing file with their own message and `STATE: kept`; the fake store accepts only a
bearer header read from stdin; the `file` source copies names raw without evaluating `$(...)`, skips unknown names,
invalid lines, names given twice and CR values, and refuses a group-readable or group-writable file, a file in the router's
writable path, a missing `SECRET_FILE` and an unknown `SECRET_SOURCE`; no secret value appears in the output. All its cases
fail against the previous script. `test/selftest_install.py` installs with `--without-mqtt`, is refused with an
`mqtt.host` while a refused run changes no program file, and runs `router.py --check` with `paho` blocked;
`test/selftest_pydeps.py` covers `--without`. `test/selftest_privilege_paths.py` still checks the access-file rules.
