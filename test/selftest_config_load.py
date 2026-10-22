#!/usr/bin/env python3
"""Self-test (Router 0.8.9): Config errors are values (Problem), and a failed reload changes nothing.

Up to 0.8.9, Config.parse() raised a ValueError mid-apply. SIGHUP caught it and reported "keeping old" - but the
config was already half-overwritten. Now: Config.load() returns Config or Problem, reload() checks first on a
probe, settings.coerce/apply return Problems. Runs without Router and network, using copies of config.example.yaml."""
import copy
import os
import shutil
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, settings   # noqa: E402
from skirnir_router.outcome import failed   # noqa: E402

FAILS = []
EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="skirnir-config-")
try:
    path = os.path.join(tmp, "config.yaml")
    shutil.copy(EXAMPLE, path)
    base = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))

    cfg = config.Config.load(path, use_overrides=False)
    check("valid configuration: Config.load returns a Config", isinstance(cfg, config.Config) and cfg.roles, type(cfg).__name__)

    bad = copy.deepcopy(base)
    bad["router"].setdefault("anthropic", {})["think"] = "vielleicht"
    yaml.safe_dump(bad, open(path, "w", encoding="utf-8"), allow_unicode=True)
    problem = config.Config.load(path, use_overrides=False)
    check("invalid (anthropic.think): Config.load returns Problem 400 with plaintext", failed(problem) and problem.status == 400
          and "anthropic.think" in problem.message, getattr(problem, "message", problem))

    bad2 = copy.deepcopy(base)
    bad2["roles"][next(iter(bad2["roles"]))]["tiers"][0]["model"] = "gibt-es-nicht:1b"
    yaml.safe_dump(bad2, open(path, "w", encoding="utf-8"), allow_unicode=True)
    problem = config.Config.load(path, use_overrides=False)
    check("invalid (role tier without catalog entry): Problem", failed(problem) and "missing from the model catalog" in problem.message,
          getattr(problem, "message", problem))

    # Reload: valid load, then a file with an error LATE in the flow (decision_engine, after roles and models)
    shutil.copy(EXAMPLE, path)
    cfg = config.Config.load(path, use_overrides=False)
    before_roles, before_lists, before_raw = dict(cfg.roles), cfg.listen, cfg.raw
    late = copy.deepcopy(base)
    late["router"]["listen"] = "127.0.0.1:19999"   # would be applied early in parse()
    late["decision_engine"] = {"enabled": True, "role": "auto", "options": ["gibt-es-nicht"], "chain": ["rules"]}
    yaml.safe_dump(late, open(path, "w", encoding="utf-8"), allow_unicode=True)
    problem = cfg.reload()
    check("Reload with late error: Problem", failed(problem) and "decision_engine" in problem.message, getattr(problem, "message", problem))
    check("... and NOTHING adopted (before: lists already new, rest old)", cfg.listen == before_lists and cfg.raw is before_raw
          and cfg.roles == before_roles, f"listen {cfg.listen}")
    shutil.copy(EXAMPLE, path)
    check("valid reload afterwards: None", cfg.reload() is None)

    # Constructor: for tests/tools without the Problem path, it still raises on invalid files
    yaml.safe_dump(bad, open(path, "w", encoding="utf-8"), allow_unicode=True)
    raised = False
    try:
        config.Config(path, use_overrides=False)
    except ValueError as e:  # exception-ok: test of the documented constructor contract
        raised = "anthropic.think" in str(e)
    check("... ValueError with the same message", raised)

    # settings: coerce/apply return Problems
    locked = {"router": {"client_auth": {"mode": "enforce", "locked": True}}}
    p = settings.coerce("router.client_auth.mode", "observe", locked)
    check("settings.coerce: locked mode -> Problem 400", failed(p) and p.status == 400 and "locked" in p.message)
    check("settings.coerce: valid -> value", settings.coerce("router.client_auth.mode", "observe", {"router": {"client_auth": {}}}) == "observe")
    p = settings.coerce("router.request_timeout_s", "schnell", {"router": {}})
    check("settings.coerce: not a number -> Problem", failed(p) and "number" in p.message)
    p = settings.apply({"router": {}}, {"router.request_timeout_s": 30, "router.ollama_poll_s": "nie"})
    check("settings.apply: first invalid value -> its Problem", failed(p) and "ollama_poll_s" in p.message)
    check("settings.apply: no mapping -> Problem", failed(settings.apply({}, ["x"])))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
