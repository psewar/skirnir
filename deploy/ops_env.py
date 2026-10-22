"""Private operational parameters for deploy and measurement scripts - from a directory OUTSIDE the repo.

Looks for `deploy.env` in $SKIRNIR_OPS, otherwise in the sibling directory `../skirnir-ops` of the repo. Optional:
  AGENT_SIGN_THUMBPRINT  SHA-1 thumbprint of the code-signing certificate; deploy.py --agent then requires a valid Authenticode
                         signature of the Windows binary (agent/sign.ps1); without an entry, only a warning.
Keys:
    HOST_FILE       File with one line <HOST_VAR>=<host or url> of the LXC host (SSH)
    HOST_VAR        Name of this variable (default LXC_HOST); HOST_USER SSH user (default root)
    HOST_PASS_ENV   Environment variable with the SSH password, populated by the secrets module (default LXC_HOST_PASS)
    CT_EXEC         Command template for 'execute in container', placeholders {ct} {cmd}; default LXD: lxc exec {ct} -- bash -lc {cmd}
    CT_PUSH         Template for 'push file to container', placeholders {ct} {src} {dst} {mode}; default LXD: lxc file push {src} {ct}{dst} --mode={mode}
    SECRETS_DIR     Directory with the secrets module (loads the SSH password, ROUTER_UI_*_PASS ... from the secret store into the environment)
    SECRETS_MODULE  Name of this module (default secret_store_env); it must provide a load() function with no arguments
    CT              Number of the LXC container where the router runs
    ROUTER_HOST     Public hostname of the router (TLS certificate is issued for this)
    UI_USER / UI_PASS_ENV   Basic-Auth account for /admin/* and the name of the environment variable with the password
In the same directory are router/config.yaml (production configuration) and router/render-env.conf (secret store access of the CT).
The repo itself only contains router/config.example.yaml.
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
    """Hostname of the LXC host from HOST_FILE (line <HOST_VAR>=host or URL)."""
    key = cfg["HOST_VAR"] + "="
    for line in open(cfg["HOST_FILE"], encoding="utf-8"):
        line = line.strip()
        if line.startswith(key):
            v = line.split("=", 1)[1].strip().strip('"')
            return v.split("://")[-1].split("/")[0].split(":")[0]
    raise SystemExit(f"{cfg['HOST_VAR']} fehlt in {cfg['HOST_FILE']}")


def load_secrets(cfg: dict):
    """<SECRETS_MODULE>.load() from SECRETS_DIR: populates <HOST_PASS_ENV>, ROUTER_UI_*_PASS etc. into os.environ."""
    if cfg["SECRETS_DIR"] not in sys.path:
        sys.path.insert(0, cfg["SECRETS_DIR"])
    import importlib
    mod = importlib.import_module(cfg.get("SECRETS_MODULE", "secret_store_env"))
    mod.load()
    return mod
