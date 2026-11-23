---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# Privacy redaction per tool format and locale pack

> Part of the portability roadmap, phase 3 ([design/portability.md](../../design/portability.md)). Extends
> [ADR-0038](0038-content-check-before-a-cloud-tier-opt-in.md) (content check before a cloud tier, opt-in) and keeps
> [ADR-0014](0014-cloud-as-a-role-tier-with-declared-data-classes.md) (declared data classes) and
> [ADR-0078](0078-privacy-threshold-from-out-of-fold-scores.md) (privacy threshold). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

The content check before a cloud tier (`router/skirnir_router/privacy.py`, called from `proxy.py` around line 801) has
three parts: rules on what the user wrote, a classifier, and redaction of house context in tool results. Two of them are
tied to one home automation system and one region:

* **Redaction knows only Home Assistant.** `redact_context` (line 170) splits a tool result into Home Assistant
  live-context entries (`- names: …` blocks), `_entry_hidden` (line 157) removes entries by `domain`, device class or
  name, and `_redact_tool_content` (line 179) expects Home Assistant's JSON with a `result` text. The defaults
  (`redact_domains`, `redact_device_classes`, `redact_tools`, lines 66-71) are Home Assistant domains and tools. A
  request from openHAB, ioBroker or a generic JSON tool reaches the cloud unredacted when its other checks pass.
* **Rules and texts are Swiss and German.** `PATTERNS` (lines 48-54) includes the Swiss AHV number and street addresses
  with German suffixes; phone numbers follow a Swiss shape; the keyword groups are German and English word starts
  (`KEYWORDS`, from line 38); the default refusal and the redaction marker are German (`DEFAULT_REFUSAL`, line 33;
  `REDACTED`, line 34). The refusal can already be configured (`refusal`, line 72), the patterns cannot.
  [ADR-0106](0106-language-dependent-defaults-live-in-the-configuration.md) moved other language-dependent defaults
  into the configuration; the privacy patterns and texts were not part of it.

The check is fail-closed for the classifier, but redaction that does not recognise a format silently passes the content
through. That is the opposite of what a user outside Home Assistant or outside Switzerland expects.

## Decision Drivers

* House context from other systems must be redacted as reliably as Home Assistant's.
* Users in other countries get patterns and texts that fit them.
* Default behaviour unchanged for existing installations (Home Assistant format, today's patterns and texts).
* Fail closed: an unknown tool format for a client that needs redaction keeps the request local rather than sending it
  unredacted.

## Considered Options

* A `ContextRedactor` per tool format and locale packs for patterns, keywords and texts.
* Keep Home Assistant only, and document that other systems should not use the privacy check.
* Let operators write their own regular expressions for everything.

## Decision Outcome

Chosen option: "`ContextRedactor` per tool format and locale packs", because it extends the protection to other systems
and regions without weakening the defaults.

* `ContextRedactor` with `redact(tool_name, content) -> (content, removed) | Problem`. Implementations: `ha_assist`
  (today's code, default), `openhab`, `jsonpath` (configured JSON paths to remove from a generic tool result). The client
  or the global privacy configuration names the redactors that apply.
* A tool result that no configured redactor recognises, for a client with redaction configured, is treated as not
  redacted: the request stays local. Today's behaviour (pass through) remains only where no redactor is configured.
* Locale packs in the repository (for example `ch-de`, `de`, `at`, `en`) bundle patterns (national identifiers, phone
  and address shapes), keyword groups, the refusal and the redaction marker. The default pack reproduces today's values;
  operator `terms` and `refusal` still override.

### Consequences

* Good, because openHAB, ioBroker and generic tool users get house-context redaction.
* Good, because the content rules fit the user's country instead of only Switzerland.
* Bad, because each locale pack needs test sentences and a maintainer who can judge them; packs without them are not
  offered.
* Bad, because the stricter "unknown format stays local" rule can keep more requests local for clients that configure
  redaction; that is the intended trade-off and must be documented.
* Bad, because the decision-embed privacy classifier is trained on German and English templates (ADR-0077); other
  languages remain a gap that a locale pack does not close.

### Confirmation

`test/selftest_privacy_check.py` stays green unchanged with the default pack and the `ha_assist` redactor. New self-tests:
a redactor per format removes the configured entries; an unrecognised tool result for a client with redaction keeps the
request local; each locale pack's patterns hit its own test sentences and the default pack matches today's `PATTERNS`,
`KEYWORDS`, refusal and marker.
