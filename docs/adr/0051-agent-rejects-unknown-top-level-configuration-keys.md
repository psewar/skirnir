---
status: proposed
date: 2026-11-08
decision-makers: maintainer
---

# The agent rejects unknown top-level configuration keys instead of renaming a retired block

## Context and Problem Statement

Before agent 0.9.0 the secret-store block of the agent configuration had another name. Agent 0.9.1 stopped reading
the old block name, and the Windows `skirnir-agent setup` renamed it to `secret_store:` when it found it, with a
message of its own. The special case lived in the setup, in a test, in a code comment and in two changelog rows, for a
name that no supported setup writes.

The agent loads its configuration with `gopkg.in/yaml.v3` (`loadConfig` in `agent/config.go`), which ignores keys that
no field reads. A block under the old name only failed when `mqtt.password_secret` needed it (the validation then
reports the missing `secret_store` fields). Otherwise it was ignored without a word, like any misspelt top-level block
(`gpu_gaurd:`, `Tunnel:`), whose settings then silently stay unused. The router's configuration check already treats
unknown keys as an error (`router/skirnir_router/config.py`).

## Decision Drivers

* A setting the agent does not read must not look as if it applied (report state explicitly, `AGENTS.md`).
* No special cases for configurations from before agent 0.9.0.
* Valid configurations keep loading. Nested keys retired in agent 0.9.1 (`tunnel.enabled`, `router.interval_s`) are
  documented as ignored.
* A self-update must not end in a restart loop on a configuration the new binary refuses.

## Considered Options

* Reject unknown top-level keys in `loadConfig` and drop the setup rename
* Keep the setup rename and keep ignoring unknown keys
* Strict decoding of every level (`yaml.Decoder.KnownFields(true)`)
* Drop the setup rename and keep ignoring unknown keys

## Decision Outcome

Chosen option: "Reject unknown top-level keys in `loadConfig` and drop the setup rename", because it treats the retired
block and every misspelt block alike, with a message that names the key and lists the known ones, and needs no list of
historical names. The known keys are derived from the YAML names of the `Config` struct, so a new block needs no second
list. A YAML merge key (`<<`) is not a key of its own and stays allowed.

Strict decoding of every level is not chosen for now: rejecting the retired nested keys would stop configurations that
load today; that can be decided on its own. Dropping the rename without the check would leave the old block silently
ignored, which the first driver rules out.

### Consequences

* Good, because a misspelt or retired top-level block stops the start with its name instead of being ignored.
* Good, because the setup, its tests and the changelog lose a special case for configurations from before agent 0.9.0.
* Bad, and breaking: a configuration with a top-level key the agent does not read no longer loads. This includes the
  secret-store block under its pre-0.9.0 name and a top-level key that only holds a YAML anchor. The self-update runs
  `check-config` with the new binary before the swap, so such a configuration ends the update as a failed order and the
  old binary keeps running; `setup` stops before it touches the service. The operator removes or renames the key
  (`secret_store:` for the old secret-store block).
* Neutral, because unknown nested keys are still ignored.

### Confirmation

`agent/config_test.go`: `TestConfigUnknownTopLevelKeyRejected` (a neutral unknown block and a key in the wrong case
are refused with their names; a merge key and a retired nested key load) and `TestConfigExampleTopLevelKeysKnown` (every
top-level block that `agent/config.example.yaml` documents is known). The updater's `check-config` step before the swap
is unchanged.
