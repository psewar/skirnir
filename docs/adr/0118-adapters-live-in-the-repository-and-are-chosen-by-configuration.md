---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# Adapters live in the repository and are chosen by configuration

> Part of the portability roadmap ([design/portability.md](../../design/portability.md)). Applies to every interface the
> roadmap introduces: status sinks (ADR-0107), backends (ADR-0110), agent runtimes (ADR-0111), GPU probes (ADR-0112),
> busy signal sources (ADR-0113), session stores (ADR-0114), memory backends (ADR-0115), wakers and release sources
> (ADR-0117) and context redactors (ADR-0119).

## Context and Problem Statement

The roadmap opens several seams so that Skirnir works without Home Assistant, NVIDIA, Ollama, PostgreSQL or Hindsight.
Each seam needs more than one implementation. The question is how an implementation gets into a running router or agent:
compiled into the repository and selected in `config.yaml`, or loaded at run time from outside (Python entry points or
import paths, Go plugins, a directory of scripts).

The project's supply-chain controls assume that the code that runs is the code in the repository:

* Python dependencies are hash-locked and installed with `--require-hashes --no-deps` (ADR-0074,
  `router/requirements-lock.txt`, `tools/security/lock_requirements.py`).
* Agent binaries are rebuilt from the tag and compared byte for byte before GitHub attests them; each release carries
  CycloneDX SBOMs (ADR-0075, `.github/workflows/release-provenance.yml`).
* Agent updates are signed by an operator key, not by the router (ADR-0020, ADR-0058, `agent/updater.go`).
* Third-party references are pinned (ADR-0055, `test/check_pinned_references.py`), and the threat model treats
  dependencies and CI as a boundary (B9, TM-16 in `docs/security/risk-register.json`).

The pattern for in-repository variants already exists: the decision engines share the abstract class `DecisionEngine`
(`router/skirnir_router/decision/base.py`, line 55), and `_build_engines` in `router/skirnir_router/decision/__init__.py`
(around line 106) builds the configured ones from `decision_engine` in the configuration.

## Decision Drivers

* The SBOM and the reproducible build must describe everything that can run.
* No new code-execution path: configuration may select code, never name code to import.
* Third parties should still be able to add an adapter.
* One way to do it, the same in router and agent.

## Considered Options

* Adapters in the repository, selected by a fixed name in the configuration.
* Python entry points or an import path in the configuration (`sink: mypackage.module:Class`).
* Go plugins (`plugin.Open`) or external helper processes per adapter.
* A generic "exec a script" adapter at every seam.

## Decision Outcome

Chosen option: "Adapters in the repository, selected by a fixed name in the configuration", because it keeps the build,
the SBOM and the signed agent binary complete, and needs no new trust decision at run time.

* Each seam has a typed interface (Python `Protocol` or abstract class; Go `interface`) and a closed table of names to
  constructors in the repository. The configuration names an entry (`kind: mqtt_ha`); an unknown name is a configuration
  `Problem` at load time (`router.py --check` fails), not a startup exception.
* Third-party adapters come as pull requests with tests, documentation in both languages and, where they cross a trust
  boundary, a security review like any other change.
* Where a seam needs to reach other software, it does so through a protocol (HTTP webhook, MQTT, a database URL), not
  through loaded code. The webhook sink (ADR-0107) and the HTTP waker (ADR-0117) are the escape hatches.

### Consequences

* Good, because `pip-audit`, the hash locks, the reproducible agent build and the SBOM keep covering all code.
* Good, because a node's signed binary decides what the agent can do; the router cannot push new behaviour through
  configuration.
* Good, because the existing decision-engine pattern becomes the single model for every seam.
* Bad, because a user who wants an unusual integration has to write a PR or use a protocol adapter instead of dropping in
  a file.
* Bad, because every adapter adds code the maintainer has to keep working.

### Confirmation

Review of each PR that adds an adapter: it is registered in the seam's table in the repository, has a self-test, and the
configuration schema (`SCHEMA` in `router/skirnir_router/config.py`) or the agent's configuration validation accepts only
the registered names. A test per seam asserts that an unknown name is refused when the configuration is loaded. No
`importlib.import_module` on configured strings and no `plugin.Open` in the code.

## More Information

Source: the portability analysis of 2026-11-22 ("deliberately not": no dynamically loaded third-party plugins).
