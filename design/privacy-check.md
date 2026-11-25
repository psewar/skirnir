# Content check before a cloud tier

How the router decides whether one request of a client with personal data may go to a cloud tier, and what it removes
before it does. Reasons: [ADR-0014](../docs/adr/0014-cloud-as-a-role-tier-with-declared-data-classes.md) (declared data
classes), [ADR-0038](../docs/adr/0038-content-check-before-a-cloud-tier-opt-in.md) (the opt-in content check),
[ADR-0043](../docs/adr/0043-configured-client-data-class-is-a-floor.md) (a configured class is a floor),
[ADR-0078](../docs/adr/0078-privacy-threshold-from-out-of-fold-scores.md) (classifier threshold),
[ADR-0099](../docs/adr/0099-institutional-providers-take-their-own-data-class-limit.md) (institutional providers),
[ADR-0119](../docs/adr/0119-privacy-redaction-per-tool-format-and-locale-pack.md) (redactors per tool format and locale packs) and
[ADR-0125](../docs/adr/0125-unrecognised-tool-results-pass-unless-the-operator-keeps-them-local.md) (unrecognised tool
results). Configuration: [docs/configuration.md](../docs/configuration.md#content-check-locale-packs-and-redactors).

## Where it runs

`proxy._Relay._via_cloud` calls `privacy.check` when `cloud.needs_privacy_check` says so: the client opted in
(`clients.<c>.privacy_check: true`), its request declared no class, and the chosen provider's limit is below the
request's class. The check sees the final payload, with memory inserted; the credential scan
(`cloud.request_block_reason`) has already run on the request, over every text the provider would receive (message
contents and content parts, tool results, tool call arguments, prompt, system). A refusal never calls the provider; the client gets the
configured refusal as an ordinary answer.

## Steps, in order

1. **Rules** (`privacy.rule_hit`) on every user message and prompt, including facts inserted from memory: the
   operator's `terms`, then the patterns and keyword groups of the locale pack (`privacy_locale.PACKS[locale]`). The
   first hit keeps the request local (`by: rules:<name>`).
2. **Classifier** (`privacy.classify`): decision-embed on loopback, `POST /privacy`. No answer, a late answer or a
   malformed one keeps the request local; a probability at or above the threshold too.
3. **Redaction** (`privacy.redact_request`) on a copy of the request, for every `tool` message:
   * a tool in `redact_tools` loses its whole result (the pack's marker);
   * otherwise the result runs through the configured redactors in order (`redactors.py`). Each returns a `Redaction`:
     the new content, how many entries it removed, whether it recognised the format, and whether the result is its to
     redact but cannot be (`unredactable`);
   * an `unredactable` result keeps the request local (`by: redaction:<redactor>`); a result no redactor recognised
     keeps it local only with `unrecognised_tool_results: keep_local` (`by: redaction:unrecognised`).

The verdict's journal entry has the outcome (`passed`, `personal`, `unavailable`, `unredacted`), the rule or reason, the
probability and the number of removed entries, never text.

## Redactors

| Name | Recognises | Removes | Cannot redact |
|---|---|---|---|
| `ha_assist` | Home Assistant's JSON envelope with a `result` text; text with live-context entries (`- names:` blocks) | entries whose `domain`, `device_class` or name is configured | JSON nested deeper than 512 levels (counted on the text before parsing, the same on every Python version) |
| `generic_json` | JSON objects and lists from tools in `generic_json.tools` (`*` = every tool) | dotted paths (`*` = every key or element) and keys at any depth | a result of a named tool that is not JSON; JSON nested deeper than 100 levels |

A redactor is a small class with `name` and `redact(tool_names, content) -> Redaction`, registered in
`redactors.REDACTORS` and built by `privacy.parse_privacy_config`. Formats are added only with test data of the real
format (openHAB and ioBroker are candidates); nothing is loaded from outside the repository.

The `generic_json` path syntax is deliberately smaller than JSONPath: dot-separated keys and `*`. It covers removing a
field from every element of a list, which is what tool results need, without a parser and its edge cases in a security
check. Keys that contain a dot cannot be addressed by path; `keys` reaches them by name.

## Locale packs

A pack (`privacy_locale.LocalePack`) holds the patterns, the keyword groups, the default refusal and the marker. `ch-de`
is the default and holds the values from before router 0.42.0; `privacy.PATTERNS`, `KEYWORDS`, `DEFAULT_REFUSAL` and
`REDACTED` are its values under their old names. `en` has e-mail, phone and IBAN patterns and English keyword starts.
The privacy classifier stays German and English (ADR-0077).

## Guarantees and tests

* The default configuration produces byte for byte the redacted body, rule hit and verdict of the code before router
  0.42.0 on a recorded corpus of synthetic payloads (`test/privacy_default_corpus.json`,
  `test/selftest_privacy_redactors.py`).
* Every pattern and keyword group of the default pack blocks, and every default domain, device class and listed tool is
  redacted (same test, and `test/selftest_privacy_check.py`).
* Configurations that would redact nothing while looking configured are refused at load: an empty `redactors` list,
  `generic_json` without its redactor or the redactor without its section, unknown names, malformed paths.
