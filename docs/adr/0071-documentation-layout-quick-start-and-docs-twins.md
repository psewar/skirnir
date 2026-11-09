---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Documentation layout: quick start README and docs twins

> Source: documentation review of the first installation.

## Context and Problem Statement

README.md and README.de.md had grown to about 780 lines each: a feature reference with a short, incomplete install
section deep inside. A new operator could not install, configure and use Skirnir from them alone, and the two languages
had drifted apart (sections and facts present in only one of them).

## Decision Drivers

* The first page leads to a working installation and a first request.
* Every fact stays available, in both languages, in one place.
* The twins stay checkable: same sections, same facts.

## Considered Options

* A short README with a quick start, and topic documents under `docs/` (install-router, install-agent, configuration,
  clients, operations, troubleshooting, reference), each with a `.de.md` twin
* Keep one long README and add a quick start at the top
* A documentation site generator

## Decision Outcome

Chosen option: "Short README plus `docs/` twins", because it puts the first install on the first page while keeping the
reference one link away, without a build step. One long README stays hard to keep in step across two languages; a site
generator adds tooling for a small project.

* README (EN/DE): what it is, architecture, quick start (try without GPU, install router, GPU node, approve, first
  request, Home Assistant), concepts, security essentials, links, development, license.
* `docs/<topic>.md` with `docs/<topic>.de.md`: the moved README sections, nothing dropped, the language-only parts of
  either README now in both, outdated statements corrected.
* AGENTS.md: the README and every `docs/` twin stay in step.

### Consequences

* Good, because the first install is a page of copyable commands.
* Good, because the drift between the languages is gone and the twin rule names every pair.
* Bad, because links into old README sections (e.g. in earlier ADRs) now point to moved content; those ADRs keep their
  historical wording.
* Neutral, because design documents stay where they are and are linked from `docs/`.

### Confirmation

A link and anchor check over README and `docs/` (all relative links resolve), the same heading structure in every
English/German pair, and `test/check_public_changes.py` over the new files.
