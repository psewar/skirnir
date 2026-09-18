"""Private Betriebsparameter fuer die Deploy- und Messskripte - aus einem Ordner AUSSERHALB des Repos.

Gesucht wird `deploy.env` in $SKIRNIR_OPS, sonst im Geschwisterordner `../skirnir-ops` des Repos. Schluessel:
    HOST_FILE       Datei mit einer Zeile <HOST_VAR>=<host oder url> des LXC-Hosts (SSH)
    HOST_VAR        Name dieser Variable (Standard LXC_HOST); HOST_USER SSH-Benutzer (Standard root)
    HOST_PASS_ENV   Umgebungsvariable mit dem SSH-Passwort, gefuellt vom Secrets-Modul (Standard LXC_HOST_PASS)
    CT_EXEC         Kommando-Vorlage fuer 'im Container ausfuehren', Platzhalter {ct} {cmd}; Standard LXD: lxc exec {ct} -- bash -lc {cmd}
    CT_PUSH         Vorlage fuer 'Datei in den Container', Platzhalter {ct} {src} {dst} {mode}; Standard LXD: lxc file push {src} {ct}{dst} --mode={mode}
    SECRETS_DIR     Ordner mit dem Secrets-Modul (laedt das SSH-Passwort, ROUTER_UI_*_PASS ... aus dem Secret-Store in die Umgebung)
    SECRETS_MODULE  Name dieses Moduls (Standard secret_store_env); es muss eine Funktion load() ohne Argumente anbieten
    CT              Nummer des LXC-Containers, in dem der Router laeuft
    ROUTER_HOST     oeffentlicher Hostname des Routers (TLS-Zertifikat lautet darauf)
    UI_USER / UI_PASS_ENV   Basic-Auth-Konto fuer /admin/* und der Name der Umgebungsvariable mit dem Passwort
Im selben Ordner liegen router/config.yaml (Produktivkonfiguration) und router/render-env.conf (Secret-Store-Zugang des CT).
Das Repo selbst enthaelt nur router/config.example.yaml.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))


def ops_dir() -> str:
    d = os.environ.get("SKIRNIR_OPS") or os.path.join(os.path.dirname(REPO), "skirnir-ops")
    if not os.path.isfile(os.path.join(d, "deploy.env")):
        raise SystemExit(f"skirnir-ops fehlt: {d}\\deploy.env (SKIRNIR_OPS setzen oder Ordner neben dem Repo anlegen, Vorlage: deploy/ops_env.py)")
    return d


def load() -> dict:
    d = ops_dir()
    cfg = {"OPS": d, "CONFIG_YAML": os.path.join(d, "router", "config.yaml"), "RENDER_ENV_CONF": os.path.join(d, "router", "render-env.conf"),
           "ROLES_YAML": os.path.join(d, "router", "roles.yaml")}
    for line in open(os.path.join(d, "deploy.env"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"')
    for k in ("HOST_FILE", "SECRETS_DIR", "CT", "ROUTER_HOST", "UI_USER", "UI_PASS_ENV"):
        if k not in cfg:
            raise SystemExit(f"deploy.env: {k} fehlt")
    cfg.setdefault("HOST_VAR", "LXC_HOST"); cfg.setdefault("HOST_USER", "root"); cfg.setdefault("HOST_PASS_ENV", "LXC_HOST_PASS")
    cfg.setdefault("CT_EXEC", "lxc exec {ct} -- bash -lc {cmd}"); cfg.setdefault("CT_PUSH", "lxc file push {src} {ct}{dst} --mode={mode}")
    return cfg


def ct_host(cfg: dict) -> str:
    """Hostname des LXC-Hosts aus HOST_FILE (Zeile <HOST_VAR>=host oder URL)."""
    key = cfg["HOST_VAR"] + "="
    for line in open(cfg["HOST_FILE"], encoding="utf-8"):
        line = line.strip()
        if line.startswith(key):
            v = line.split("=", 1)[1].strip().strip('"')
            return v.split("://")[-1].split("/")[0].split(":")[0]
    raise SystemExit(f"{cfg['HOST_VAR']} fehlt in {cfg['HOST_FILE']}")


def load_secrets(cfg: dict):
    """<SECRETS_MODULE>.load() aus SECRETS_DIR: fuellt <HOST_PASS_ENV>, ROUTER_UI_*_PASS usw. in os.environ."""
    if cfg["SECRETS_DIR"] not in sys.path:
        sys.path.insert(0, cfg["SECRETS_DIR"])
    import importlib
    mod = importlib.import_module(cfg.get("SECRETS_MODULE", "secret_store_env"))
    mod.load()
    return mod
