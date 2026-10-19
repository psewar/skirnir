# Repository guidance

- Keep optional services disabled by default and preserve existing inference protocols.
- Use synthetic data only in source, tests, fixtures, documentation, commits and pull requests.
- Never commit personal/family details, real user prompts, private hostnames, network
  layouts, device identifiers, credentials, connection strings or deployment-specific paths.
- Read service addresses and secrets from environment variables or an external secret store.
  Use loopback addresses and reserved example domains/ranges for examples and tests.
- Before every commit/publication, inspect added lines and staged files for identifying
  content and secrets. Keep any private matching rules outside the repository.
- Do not test against personal infrastructure or import real memory/session content.
- Run the relevant self-tests, lint, config validation and applicable integration tests.

## Explicit control flow: exceptions are not a control signal

Applies to all code in this repository, to every agent and every contributor.

- **Report state as a fact.** A component that knows something about itself or about a
  request reports it as a return value, status or field. Expected conditions (invalid input,
  missing grant, unknown session, conflicting event, full queue, unavailable backend) are
  values: in the router a `Problem` (`router/skirnir_router/outcome.py`) with the HTTP status
  and a fixed message chosen where the condition is detected. Callers test `failed(value)`
  and pass the problem on. Go code returns errors as values, as the language intends.
- **Check first, then act.** Validate format, existence, permission and availability with
  explicit checks (patterns, `isinstance`, ranges, `queue.full()`, `importlib.util.find_spec`),
  not by attempting the operation and catching its failure. Example: match a UUID's text
  form before `UUID(...)`, instead of catching `ValueError`.
- **`try/except` only for real exceptions**: faults that cannot be checked beforehand or whose
  check would race (network, database, OS and file system, external processes, cancellation,
  parsing untrusted JSON). Catch them at that boundary only and turn them into a `Problem`
  there (e.g. `session_types.database`, `hindsight.HindsightClient._exchange`). Nothing above
  the boundary sees the exception.
- **Never parse error text** (`str(exc)`, `repr(exc)`, `exc.args`, substrings of messages) to
  decide anything. If cases must be distinguished, give them a status, a field or a fixed
  category (`Problem.kind`).
- **Allowed exceptions** besides boundaries: programming errors (a violated internal invariant)
  and aborting startup, where aiohttp only accepts an exception from a cleanup context.
- **Mark every remaining site.** Each `except` and `raise` in router code carries
  `# exception-ok: <reason>` on the same line or on the comment line directly above.
  `test/check_control_flow.py` enforces this in CI and also flags error-text comparisons.
  Code not yet converted has a per-file ratchet in `test/control_flow_baseline.json`: the
  count may only go down, and a change that lowers it updates the baseline
  (`python test/check_control_flow.py --update`). New files start at zero.
- **Tests** assert expected outcomes as returned values (`self.assertEqual(result.status, 409)`).
  `assertRaises` is for genuine exceptions only (cancellation, programming errors, startup abort).
