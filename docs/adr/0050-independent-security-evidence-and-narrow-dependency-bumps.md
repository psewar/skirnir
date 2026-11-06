---
status: proposed
date: 2026-11-06
---
# Verify security evidence independently of pull-request code

The PR workflow and its tests run candidate code. Checking a second worktree in the same job does not isolate
the later check. Retain the PR checks for feedback. An independent default-branch workflow verifies PR Git objects
on a clean hosted runner. A dedicated App in a restricted environment can publish the result on the exact SHA from
a separate job. The maintainer binds that App as the expected source of a required status. Removing CI then leaves
a new head without a required status. Endpoint/model configuration is irrelevant to this deterministic gate.

Refine ADR-0047's dependency exception to verifiable version changes of existing actions or Go dependencies.
Broad, unrecognized and provenance-changing edits require ordinary fresh review evidence, including on bot PRs.
The stricter exception favors manual review when the parser cannot establish that only the version changed.

Validate with a PR fixture that replaces its checker and deletes its workflow, a concurrent ref change and negative
tests for action swaps, pin weakening, changed Go sources/checksums, added manifests and executable modes.
Deployment details and administrator acceptance checks are in design/continuous-security-review.md.
