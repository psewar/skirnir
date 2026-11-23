---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# Unrecognised tool results pass unless the operator keeps them local

> Refines [ADR-0119](0119-privacy-redaction-per-tool-format-and-locale-pack.md) (privacy redaction per tool format and locale pack) on one point: when a tool
> result that no redactor recognises keeps the request local. Keeps
> [ADR-0038](0038-content-check-before-a-cloud-tier-opt-in.md) (content check before a cloud tier, opt-in) and
> [ADR-0043](0043-configured-client-data-class-is-a-floor.md) (a configured client class is a floor) unchanged.

## Context and Problem Statement

ADR-0119 introduces redactors per tool format (`ha_assist`, `generic_json`) and decides: "A tool result that no
configured redactor recognises, for a client with redaction configured, is treated as not redacted: the request stays
local. Today's behaviour (pass through) remains only where no redactor is configured."

Every installation that uses the content check has a redactor configured, because the Home Assistant redaction
(`ha_assist`) has always been on. Applied literally, the rule would change the default: tool results that the router
passed until now would keep requests local. That includes plain text, a weather tool, and Home Assistant's own intent
results (`HassTurnOn` answers with JSON that has no live-context `result` text). The voice assistant that ADR-0038 was
made for would refuse after almost every action. The implementation must not change the default behaviour, and it must
not weaken it either.

## Decision Drivers

* The default behaviour stays exactly as it was (verified byte for byte on a corpus of synthetic payloads).
* No change may make the default weaker. Anything new may only be stricter, and only when the operator chooses it.
* An operator who wants the strict rule of ADR-0119 must be able to get it, with a way to name tools whose results are
  harmless.
* A tool the operator explicitly named for redaction must never leave unredacted.

## Considered Options

1. An explicit key `router.cloud.privacy_check.unrecognised_tool_results: pass | keep_local`, default `pass`. A tool
   named in `generic_json.tools` whose result cannot be redacted (not JSON) keeps the request local in both modes.
2. The rule of ADR-0119 as written: strict as soon as any redactor is configured, which includes the default.
3. Strict only when the operator writes the `redactors` key, even if its value is the default.

## Decision Outcome

Chosen option: 1, because it is the only one that keeps the default and still offers the strict rule. Option 2 changes
the default for every existing installation. Option 3 makes the meaning of a configuration depend on whether a key that
holds its default value is written down, which is a silent switch.

* `pass` (default): a tool result that no redactor recognises is sent as it is, as before router 0.42.0.
* `keep_local`: such a result keeps the request local; the client gets the refusal, and the journal records
  `result: unredacted`, `by: redaction:unrecognised`. Recognised are: Home Assistant's JSON envelope with a `result`
  text, text with live-context entries, a tool in `redact_tools`, and a JSON result of a tool listed in
  `generic_json.tools` (an empty path list marks a tool as harmless).
* In both modes a result of a tool named in `generic_json.tools` that is not JSON keeps the request local
  (`by: redaction:generic_json`): the operator asked for it to be redacted, and it cannot be.
* The key is set in `config.yaml` only, like the rest of `privacy_check`; the web UI cannot change it.

### Consequences

* Good, because existing installations behave exactly as before.
* Good, because an operator with tools whose formats no redactor knows can choose to keep them local, and can list
  harmless tools.
* Bad, because the default still sends tool results of formats no redactor knows. The content check protects what the
  user wrote and the house context in known formats; for other tools the operator decides. The documentation says so.
* Bad, because `keep_local` with Home Assistant keeps requests local after intent results unless the operator lists
  those tools for `generic_json`.

### Confirmation

`test/selftest_privacy_redactors.py` (in CI): the default corpus gives the same bytes as before; `keep_local` keeps
plain text, intent JSON, non-text and empty results local and lets recognised ones and listed tools pass; a named
`generic_json` tool with a non-JSON result keeps the request local in the default mode, and the proxy then answers with
the refusal without calling the provider.
