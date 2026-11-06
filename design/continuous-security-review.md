# Continuous security review

Decision: [ADR-0047](../docs/adr/0047-change-bound-security-review-before-pull-requests.md) (accepted).
The [threat model](../docs/security/threat-model.md) records boundaries and scenarios; the
[risk register](../docs/security/risk-register.json) records open/mitigated/accepted status and incomplete test coverage.
The repository [security-review skill](../.agents/skills/skirnir-security-review/SKILL.md) is used by coding agents and
can also be followed by human reviewers. It is checked into this repository, not installed as a personal ChatGPT skill.

## Before creating or updating a PR

1. Read AGENTS.md and execute the skill. Compare the complete diff with its merge-base, follow callers/data flows,
   map affected B1–B9 boundaries and TM IDs, fix introduced findings and run relevant negative tests.
2. Stage intended files with `git add <paths>`. Resolve unstaged/untracked files; the helper refuses to ignore them.
3. Run `python tools/security/review.py snapshot --base origin/main --head INDEX`. Copy `base_sha`, `change_digest`
   and the changed-path list. No model is called by this command.
4. Write `docs/security/reviews/current.json` with actual evidence, then stage it. Do not generate an empty passing
   record without a review. All changes, even documentation changes, require a record; keep small reviews small.
   Exception: a PR whose author GitHub reports as `dependabot[bot]` cannot write a record. CI then runs the checker
   with `--dependency-update`: the change may only touch dependency manifests (`go.mod`, `go.sum`,
   `requirements*.txt`) and `uses:` lines in workflow files; anything else fails and needs a normal review. The
   maintainer reviews the version bump itself. The author comes from the GitHub event, never from commit metadata.
5. Run `python tools/security/review.py check --base origin/main --head INDEX` and `python test/check_public_changes.py`.
   Commit and open the PR with the review summary, tests, limitations and TM IDs. Further changes require re-review.

The digest covers the merge-base commit and **all** target-tree paths, file modes, types and blob IDs, excluding only
the fixed record path. This avoids the self-hash cycle and includes deletions, renames, dependencies and review-policy
changes. Only a regular non-executable JSON record may be excluded. The staged tree and resulting commit have the same
digest. Rebase/change of merge-base invalidates it. Another commit with identical content does not. The gate compares
the PR head, while ordinary integration CI still tests the GitHub merge commit. A base advance after divergence is not
itself a re-review of the combined merge result: use up-to-date branch requirements/rebase before merging where needed.

Example record shape (replace placeholders; this example cannot pass):

```json
{
  "schema_version": 1,
  "status": "completed",
  "base_sha": "copy exact snapshot base_sha",
  "change_digest": "copy exact snapshot change_digest",
  "reviewer": "reviewer role or agent name; no private identifiers",
  "summary": "What changed, which paths were traced, what was found and fixed",
  "reviewed_paths": ["every changed path from snapshot, plus surrounding files inspected"],
  "boundaries": ["B1", "B6"],
  "risk_ids": ["TM-01", "TM-05"],
  "findings": [],
  "tests": [{"command": "actual command", "result": "passed", "evidence": "actual observed result"}],
  "limitations": ["State untested paths and deployment assumptions explicitly"]
}
```

Tests may instead use `result: not_run` with a concrete reason in `evidence`; a required failed test cannot be marked
passed or hidden here. Findings require `id`, `evidence`, `resolution` and `disposition`:
`fixed` for verified fixes; `baseline` only with an existing TM ID and explanation that this change does not introduce
it; `accepted` only with a matching accepted register entry. Open findings fail the record check.
An acceptance requires `approved_by`, `rationale`, `scope`, `compensating_controls`, `expires` (YYYY-MM-DD), and the
maintainer's actual approval. Expiry on or before today fails. Never auto-accept the baseline just to make CI green.

The record proves **shape and freshness**, not that the reviewer is truthful, that tests ran, or that a model found
every vulnerability. A malicious author can misrepresent a finding as baseline; maintainer review remains necessary.
Protect workflow, skill, checker, threat-model and register changes through branch rules and explicit review.
The single current record is intentionally replaced per PR; historical records remain in Git history. Concurrent PRs
may conflict on this file: rerun the review on the final rebased tree, do not blindly pick one side.

## CI and trusted review boundary

`threat-model.yml` runs on every PR, main push, weekly and manual dispatch. The PR job validates evidence twice: with
the checker as the PR changed it, and with the checker of the PR's base commit (a separate worktree of the base, run
with `python -I` against the candidate's Git objects). The PR can weaken its own copy but not the base copy, so
**Security review evidence** stays a trustworthy required check; the job has no secrets and only read access. The PR
that introduces the checker has no base copy yet and says so in its log. Main/scheduled runs validate the
model/register and expired acceptances, not a historical record against a new merge commit.
`selftest_threat_review.py` exercises the checker, and the existing Security workflow runs the extended runtime
negative tests, pip-audit and govulncheck. The normal CI explicitly includes the new checker tests too.

`threat-review.yml` runs **from the trusted default branch** after the ordinary CI workflow completed successfully for
a PR from this repository. Fork PRs, failed CI runs and Dependabot runs are skipped: `workflow_run` receives Actions
secrets even for forks, so otherwise anyone could spend the model key by opening PRs.
It fetches only the candidate's Git objects by validated SHA; no candidate checkout, imports, tests, dependencies,
actions, hooks or artifacts are executed/loaded. `python -I` isolates imports. The default-branch checker revalidates
evidence and then optionally requests an advisory model review. No candidate SKILL/AGENTS file becomes an instruction.
The introducing PR cannot use a default-branch checker that has not been merged yet; review it locally and through PR CI.
Future changes to the checker still have to satisfy the previous trusted version or be migrated explicitly.

All new workflows have `contents: read`, pinned Action commits, disabled persisted checkout credentials and timeouts.
There is no PR comment/status publishing, repository write, production access, candidate cache/artifact loading or
model tool execution. The workflow-run check appears in its own Actions run, not as a PR status, so it cannot be a required check; the
base-checker step above is the trusted gate. The maintainer should require **Security review evidence** and existing
security/CI jobs in branch protection. Repository rulesets are not changed by this PR.

## Optional model execution

No provider, credential, source-code disclosure or recurring model spend is enabled by default. Configure repository
Actions variables only after choosing the processing destination:

| Setting | Meaning |
| --- | --- |
| `SKIRNIR_SECURITY_AI_ENABLED` | Literal `true` opts into sending source and policy context to the selected endpoint. |
| `SKIRNIR_SECURITY_AI_URL` | Full OpenAI-compatible chat-completions URL, e.g. `https://model.example.net/v1/chat/completions`. No appended path; HTTPS required except literal loopback HTTP for local CLI tests/use. No userinfo, query, fragments or redirects. |
| `SKIRNIR_SECURITY_AI_MODEL` | Model identifier on that endpoint; choose a model/window suitable for security reasoning. |
| `SKIRNIR_SECURITY_AI_KEY` | Optional Actions **secret** (Bearer token), never a committed file. Use a dedicated limited model credential. |

The client sends `model`, `messages`, `stream:false`, `max_tokens:4096` and expects one completed chat choice containing
the skill's JSON schema. No function/tool interface is supplied. Endpoint/model compatibility is operator-verified;
an unsupported request, timeout, truncation, non-JSON answer or tool-call attempt produces `unavailable`, never clean.
Transport failures do not print URL, key, response body or raw model text. HTTPS certificate checks remain enabled.

GitHub-hosted runners cannot reach an ordinary private loopback service on another host. For a local model, run the
same command on a controlled local machine with a **trusted** checkout and candidate objects fetched but not checked
out. Do not put untrusted PR jobs on a persistent self-hosted machine with production access. This PR does not provision
a runner, VPN or publicly expose a model server. Use a reachable explicitly approved endpoint for the provided workflow.

```bash
# Credentials/endpoint/model are supplied through the environment, not command-line arguments or repository files.
python -I tools/security/review.py ai --base origin/main --head HEAD --output /tmp/threat-advisory.json
python -I tools/security/review.py ai --base HEAD --head HEAD --full --output /tmp/threat-full.json
```

For an automated PR review the helper reads before/after changed text, anchor controls and static callers/references.
This is not a complete call graph. Full weekly/manual mode inventories all tracked files (except the review record).
Files are read via Git blobs, not followed as symlinks. Binary/special/over-512-KB files are listed as omitted.
Text is chunked, never silently cut; a default 12-batch budget limits cost/time. CLI `--max-batches 1..100` adjusts the
budget after evaluating model cost and timeout. The artifact records total/reviewed batches and omitted scope.
Large reviews can be `partial`, requiring a further/manual review; no completeness claim is made across chunks.

Results are `skipped` (disabled), `unavailable` (configuration/transport/schema failure), `partial` (omitted/budget-limited)
or `completed` (all selected batches processed, still not proof of safety). Findings and limitations remain advisory;
the process does not auto-approve, auto-fix, accept risks or block based on model judgment alone. It also does not write
a pre-PR attestation. Raw JSON is uploaded for seven days, never rendered as workflow commands or PR Markdown. Treat it
as untrusted and potentially sensitive; follow SECURITY.md and redact before any public discussion. Public repository
Actions artifacts may be accessible to others; do not send private code/data to this workflow.

## Validation and limits

Run `python test/selftest_threat_review.py`, `python test/selftest_security.py`, `python test/check_adr_index.py`,
`ruff check .`, the evidence check and the public-data scan. The test suite uses disposable Git repositories and a
synthetic loopback redirect server. Negative cases verify stale digest, unstaged/untracked files, deletions/modes,
review policy changes, missing coverage, acceptance expiry, symlinks/oversized blobs, disabled networking and chunking.
The security tests additionally cover wrong-token/IP fallback, concrete-model/role denial, cloud opt-out and pending cap.
No production system, real model provider, downstream tool agent, GitHub ruleset or live CI schedule is tested locally.

Operational assumptions, missing HTTPS enforcement in the runtime cloud configuration, full denial-of-service
protection, rootless deployment and update anti-rollback remain baseline work; this tooling PR does not silently fix
or accept them. Update the model/register and add rejected-case tests when those behaviors change.
