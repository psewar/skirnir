---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Further secret keys named in render-env.conf

> Source: adding an OpenAI-compatible cloud provider (Mistral's EU endpoint) as a cloud tier ahead of an existing one.

## Context and Problem Statement

A further OpenAI-compatible cloud provider needs no router code: `kind: openai`, a `base_url`, the host in
`router.cloud.egress_allow` and catalogue entries are configuration. Its key, however, has to reach
`/etc/skirnir-router/secrets.env`, and `render-env.sh` fetched only a fixed list of names from the secret store
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and the memory-layer secrets). Every new provider would have needed a code change and
a release, or a hand-edited `secrets.env` that the next timer run overwrites.

`secrets.env` is the `EnvironmentFile` of the router unit, which runs as root. Whatever name ends up there becomes part of
that process's environment.

## Decision Drivers

* Adding a provider stays configuration, not a code change.
* `secrets.env` keeps coming only from the secret store, rendered by root; the router still cannot widen what is rendered
  (`render-env.conf` lives in the root-only folder of ADR-0073).
* A name from the configuration must not let a value from the secret store steer the root process (`LD_PRELOAD`,
  `PATH`, `PYTHONPATH`, ...), and must not become part of a URL unchecked.
* A missing key is visible, never its value.

## Considered Options

* A list of further names in `render-env.conf` (`EXTRA_SECRET_KEYS`), restricted to key-like names
* Render every secret in the project
* Derive the names from `api_key_env` in `config.yaml`
* Add each new provider's key to the fixed list in `render-env.sh`

## Decision Outcome

Chosen option: "`EXTRA_SECRET_KEYS` in `render-env.conf`", because it keeps the decision with root in the folder the
router cannot write and needs no code per provider.

* `render-env.sh` reads `EXTRA_SECRET_KEYS` (space-separated) after the built-in names, without globbing. A name must start
  with a capital letter, contain only capital letters, digits and `_`, and end in `_KEY` or `_TOKEN`; anything else is
  reported and skipped. A name that is already built in is not fetched twice. A name missing in the secret store is reported
  by name and skipped.
* Every value `render-env.sh` writes - built-in or further - must be a single line. A value with a line break would start
  a further variable of the secret-store writer's choosing in the root unit's environment and make the name rule
  pointless; such a value is reported by name and skipped (`MQTT_PASSWORD`: nothing is rendered and the existing
  `secrets.env` stays). This also closes the same path for the built-in names, which existed before.
* `config.example.yaml` shows a disabled `mistral` provider with the EU endpoint and its `egress_allow` entry.

Rendering every secret would put unrelated values (and arbitrary names) into the root unit's environment. Deriving the
names from `config.yaml` would let anyone who can change the router's configuration - the admin UI writes it - decide which
secrets root renders, which crosses the boundary ADR-0073 drew. A fixed list per provider is the status quo this replaces.

### Consequences

* Good, because a further OpenAI-compatible provider is a configuration change plus one name in `render-env.conf`.
* Good, because only key-like names reach the root unit's environment, and the selection stays with root.
* Bad, because an operator has to name the key in two places (`api_key_env` in `config.yaml`, `EXTRA_SECRET_KEYS` in
  `render-env.conf`); a mismatch shows as a provider without a key (no tier), as for a missing built-in key.
* Neutral, because the suffix rule is a name rule, not a value check: a secret named `..._KEY` still has to be a key the
  operator meant to render.

### Confirmation

`test/selftest_render_env.py` runs `render-env.sh` against a fake secret store in CI: without `EXTRA_SECRET_KEYS` only the
built-in keys are fetched; named keys are rendered in order; invalid names (`bad-name`, a leading digit, `*`), names without
the suffix (`LD_PRELOAD`, `PATH`, `PYTHONPATH`) and a built-in name are refused or not fetched again; a missing name is
reported; values with LF or CR (built-in, further and `MQTT_PASSWORD`) never reach `secrets.env`; no secret value appears
in the output. A copy of the script without the suffix rule and one without the line check both fail the test. `router.py --check router/config.example.yaml --pure` accepts the example.
