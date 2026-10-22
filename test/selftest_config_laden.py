#!/usr/bin/env python3
"""Selbsttest (Router 0.8.9): Konfigurationsfehler sind Werte (Problem), und ein gescheitertes Neuladen aendert nichts.

Bis 0.8.9 warf Config.parse() mitten im Uebernehmen einen ValueError. SIGHUP fing ihn ab und meldete "keeping old" - die
Konfiguration war da aber schon halb ueberschrieben. Jetzt: Config.load() liefert Config oder Problem, reload() prueft erst an
einer Probe, settings.coerce/apply liefern Probleme. Laeuft ohne Router und Netz, mit Kopien von config.example.yaml."""
import copy
import os
import shutil
import sys
import tempfile

import yaml

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "router"))
from skirnir_router import config, settings   # noqa: E402
from skirnir_router.outcome import failed   # noqa: E402

FAILS = []
EXAMPLE = os.path.join(HIER, "..", "router", "config.example.yaml")


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
    check("gueltige Konfiguration: Config.load liefert ein Config", isinstance(cfg, config.Config) and cfg.roles, type(cfg).__name__)

    bad = copy.deepcopy(base)
    bad["router"].setdefault("anthropic", {})["think"] = "vielleicht"
    yaml.safe_dump(bad, open(path, "w", encoding="utf-8"), allow_unicode=True)
    problem = config.Config.load(path, use_overrides=False)
    check("ungueltig (anthropic.think): Config.load liefert Problem 400 mit Klartext", failed(problem) and problem.status == 400
          and "anthropic.think" in problem.message, getattr(problem, "message", problem))

    bad2 = copy.deepcopy(base)
    bad2["roles"][next(iter(bad2["roles"]))]["tiers"][0]["model"] = "gibt-es-nicht:1b"
    yaml.safe_dump(bad2, open(path, "w", encoding="utf-8"), allow_unicode=True)
    problem = config.Config.load(path, use_overrides=False)
    check("ungueltig (Rollenstufe ohne Katalogeintrag): Problem", failed(problem) and "missing from the model catalog" in problem.message,
          getattr(problem, "message", problem))

    # Neuladen: gueltig geladen, dann eine Datei mit einem Fehler SPAET im Ablauf (decision_engine, nach Rollen und Modellen)
    shutil.copy(EXAMPLE, path)
    cfg = config.Config.load(path, use_overrides=False)
    vorher_roles, vorher_listen, vorher_raw = dict(cfg.roles), cfg.listen, cfg.raw
    late = copy.deepcopy(base)
    late["router"]["listen"] = "127.0.0.1:19999"   # wuerde frueh in parse() uebernommen
    late["decision_engine"] = {"enabled": True, "role": "auto", "options": ["gibt-es-nicht"], "chain": ["rules"]}
    yaml.safe_dump(late, open(path, "w", encoding="utf-8"), allow_unicode=True)
    problem = cfg.reload()
    check("Neuladen mit spaetem Fehler: Problem", failed(problem) and "decision_engine" in problem.message, getattr(problem, "message", problem))
    check("... und NICHTS uebernommen (vorher: listen schon neu, Rest alt)", cfg.listen == vorher_listen and cfg.raw is vorher_raw
          and cfg.roles == vorher_roles, f"listen {cfg.listen}")
    shutil.copy(EXAMPLE, path)
    check("gueltiges Neuladen danach: None", cfg.reload() is None)

    # Konstruktor: fuer Tests/Werkzeuge ohne Problem-Pfad wirft er bei ungueltiger Datei weiter
    yaml.safe_dump(bad, open(path, "w", encoding="utf-8"), allow_unicode=True)
    raised = False
    try:
        config.Config(path, use_overrides=False)
    except ValueError as e:  # exception-ok: test of the documented constructor contract
        raised = "anthropic.think" in str(e)
    check("... ValueError mit derselben Meldung", raised)

    # settings: coerce/apply liefern Probleme
    locked = {"router": {"client_auth": {"mode": "enforce", "locked": True}}}
    p = settings.coerce("router.client_auth.mode", "observe", locked)
    check("settings.coerce: gesperrter Modus -> Problem 400", failed(p) and p.status == 400 and "locked" in p.message)
    check("settings.coerce: gueltig -> Wert", settings.coerce("router.client_auth.mode", "observe", {"router": {"client_auth": {}}}) == "observe")
    p = settings.coerce("router.request_timeout_s", "schnell", {"router": {}})
    check("settings.coerce: keine Zahl -> Problem", failed(p) and "number" in p.message)
    p = settings.apply({"router": {}}, {"router.request_timeout_s": 30, "router.ollama_poll_s": "nie"})
    check("settings.apply: erster ungueltiger Wert -> sein Problem", failed(p) and "ollama_poll_s" in p.message)
    check("settings.apply: kein Mapping -> Problem", failed(settings.apply({}, ["x"])))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
