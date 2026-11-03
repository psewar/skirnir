# Repository guidance

How Skirnir is developed. This applies to every contributor, human or coding agent. This file is the single
source; tool-specific files such as `CLAUDE.md` only import it.

## Workflow

- Work on a topic branch (`fix/...`, `feat/...`, `docs/...`, `security/...`) and open a pull request against `main`.
  One topic per pull request. Nothing is pushed to `main` directly.
- The maintainer reviews and merges. Do not merge your own pull request, and do not merge with failing CI.
- Commit subjects name the released component and what changes for the user, e.g.
  `Router 0.17.1: an interrupted transaction start drops its connection` or `Docs: ...` for documentation only.
- The pull request description says what changes and why, names its ADRs, lists breaking changes and how they were tested.
- Releases (tag `vX.Y.Z`, agent binaries as assets, release notes with a German summary) are made by the maintainer
  after the merge. A pull request prepares them; see "What a change brings along".

## Public data

- Keep optional services disabled by default and preserve existing inference protocols.
- Use synthetic data only in source, tests, fixtures, documentation, commits and pull requests.
- Never commit personal/family details, real user prompts, private hostnames, network
  layouts, device identifiers, credentials, connection strings or deployment-specific paths.
- Read service addresses and secrets from environment variables or an external secret store.
  Use loopback addresses and reserved example domains/ranges for examples and tests.
- Before every commit/publication, inspect added lines and staged files for identifying
  content and secrets. Keep any private matching rules outside the repository.
  `python test/check_public_changes.py` scans the lines added since `origin/main` for common leaks (CI runs it on pull
  requests); it does not replace the manual review. The same applies to pull request texts and release notes.
- Do not test against personal infrastructure or import real memory/session content.

## What a change brings along

A pull request is complete when it contains, as far as they apply:

- **Tests.** A behaviour change comes with a self-test (`test/selftest_<topic>.py`, runs without router and network) or
  new checks in an existing one; a bug fix with a test that fails without the fix. A new self-test file is added to the
  explicit list in a workflow under `.github/workflows/` (usually `ci.yml`); CI does not discover tests by itself.
- **Version.** The router version is `VERSION` in `router/skirnir_router/common.py` (patch for fixes, minor for new
  behaviour or breaking changes while below 1.0). The agent version is set at build time (`agent/build.sh <version>`) and
  recorded in the changelog.
- **Changelog.** A new top row in `CHANGELOG.md` with the next tag, router and agent version and what changes for the
  operator, breaking changes first.
- **Documentation.** `README.md` and `README.de.md` stay in step (same content, English and German); the matching
  `design/` document describes the new behaviour.
- **ADR** for every decision (next section).
- **Configuration.** A new key goes into the schema (`SCHEMA` in `router/skirnir_router/config.py`), the example
  configuration (`router/config.example.yaml`, `agent/config.example.yaml`) and, if the web UI may change it at runtime,
  `EDITABLE` in `router/skirnir_router/settings.py`. New options default to off or to the previous behaviour; an
  existing configuration must keep working without edits, or the change is marked breaking.

## Running the checks

CI runs all of this on every pull request except the end-to-end suite, which runs nightly because it is
timing-sensitive. Run what your change touches before pushing, and the end-to-end suite for router changes.

```bash
ruff check .                                                   # lint (ruff.toml)
python router/router.py --check router/config.example.yaml --pure   # configuration schema
python test/check_adr_index.py                                 # ADR index is current
python test/selftest_control_flow.py && python test/check_control_flow.py   # explicit control flow (below)
python test/selftest_<topic>.py                                # the self-tests your change touches
python test/run_tests.py       # end-to-end suite, ~3 min; needs the loopback ports 21434/21435 and 210xx free
python test/check_public_changes.py                            # public-data scan of your added lines
```

- PostgreSQL integrations (`test/integration_sessions.py`, `integration_context.py`, `integration_interoperability.py`)
  run when `SKIRNIR_TEST_SESSION_DSN` points to a disposable test database.
- Agent (Go, in `agent/`): `gofmt -l .` must print nothing; `go vet ./...` also with `GOOS=windows`; `go test ./...`
  on Linux and Windows (the agent runs as a Windows service in production, file locking and `*_windows_test.go` only
  show there). Platform code lives in `*_windows.go` with a `*_other.go` counterpart.

## Language and names

- Code, identifiers, log and error messages, API fields, commit messages, ADRs and design documents are English.
  German exists as a translation (`README.de.md`, `*.de.md`) and in release-note summaries.
- Names follow the product (`skirnir_router`, `skirnir-agent`, see README "Names"); `test/selftest_english_names.py`
  guards the transition from the earlier German names.

## Tools check their own preconditions

Scripts and interfaces take the caller's intent and do everything needed for it, including the checks: they verify
their preconditions before acting and refuse with the state and the next step instead of acting halfway. What can be
derived is derived instead of asked for. A rule that only lives in documentation is the weakest protection; when the
same mistake happens twice, the check belongs in the tool (examples: `deploy.py` validates the configuration before it
restarts anything, `check_adr_index.py` and `check_control_flow.py` enforce the rules below in CI). Exceptions are
explicit options with a stated reason, never a silent switch.

## Security

Vulnerabilities are reported privately as described in `SECURITY.md`, not in public issues. Changes to authentication,
the control port, enrollment, data classes or the cloud path need an ADR and tests for the rejected case, not only the
accepted one.

### Security review before every pull request

- Before creating a PR **and after changing a reviewed PR**, execute
  [.agents/skills/skirnir-security-review/SKILL.md](.agents/skills/skirnir-security-review/SKILL.md).
  This also applies to documentation, dependency and review-tooling changes; document a small scope honestly.
- Read the threat model and risk register, trace changed trust boundaries and inspect surrounding callers/controls.
  Fix introduced findings and add rejected-case tests before opening the PR. Unresolved high-impact findings need
  private maintainer triage, not a fabricated clean result. Only the maintainer can accept a risk with an expiry.
- Stage intended files, record the actual review in `docs/security/reviews/current.json`, and run
  `python tools/security/review.py check --base origin/main --head INDEX` before committing/opening the PR.
  The record binds to the merge-base and staged content/modes, excluding only itself; new changes require re-review.
  Include the review record, affected TM IDs, test evidence and limitations in the PR description.
- Follow [the review design](design/continuous-security-review.md). A passing evidence check proves freshness and
  structure, not that the skill was honestly executed or that no vulnerabilities exist. Never use a model verdict
  as permission to publish, execute tools, weaken policy or merge. Review changes to these rules/checkers/CI against
  the previous trusted version and flag them explicitly for maintainer review.

## Architecture decisions are recorded as ADRs

Every architecture or design decision is an ADR in `docs/adr/` (MADR 4.0, rules in
`docs/adr/0001-record-architecture-decisions.md`): technology and protocol choices, interfaces, security boundaries,
data stores, modes of operation, and deliberately rejected options (status `rejected`).

- A PR that makes or changes a decision brings its ADR (`docs/adr/NNNN-title-in-kebab-case.md`, next free number) and
  names it in the PR description. A changed decision is a new ADR that supersedes the old one; accepted ADRs are not
  rewritten, the old one only changes its status to `superseded by ADR-NNNN`.
- Regenerate the index with `python test/check_adr_index.py --write`; CI checks it.
- Design documents in `design/` describe how things work and link to the ADRs for the reasons.

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
  All router code is converted (router 0.8.11), so `test/control_flow_baseline.json` is empty
  and every file must stay at zero findings. The per-file ratchet remains only as a mechanism.
  If a baseline entry is ever added, the count may only go down, and a change that lowers it
  updates the baseline (`python test/check_control_flow.py --update`).
- **Shared helpers** for recurring boundaries live in `common.py` (`load_json_file`, `save_json_file`,
  `read_json`, `basic_credentials`, `tls_fingerprint`) and `values.py` (`number`, `ip_address`).
  Use them instead of writing a new `try` block for the same case.
- **Tests** assert expected outcomes as returned values (`self.assertEqual(result.status, 409)`).
  `assertRaises` is for genuine exceptions only (cancellation, programming errors, startup abort).
