"""Self-test (ADR-0073, TM-14): the paths from a compromised router to host privileges stay closed.

1. The root secret renderers (router/render-env.sh, monitoring/render-monitoring.sh) source their access file only from
   outside the router's writable paths, owned by root and not group/other writable - also when reached through a symlink,
   and the same for SECRET_STORE_AUTH_FILE. Run in a temporary tree with the scripts' test overrides (Linux, bash).
2. The units: the router sees the certificate folder read-only and the secret-store folder not at all; the root helper
   units carry the hardening; deploy.py places the access file outside every ReadWritePaths of the router unit.
3. Parity with the agent: every Ollama path the router sends through the tunnel is in the agent's passthrough list
   (agent/passthrough.go), and no path the router forbids for clients is.
    python test/selftest_privilege_paths.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "router"))
FAILS = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def unit_values(text, key):
    return [v.strip() for k, v in re.findall(r"^(\w+)=(.*)$", text, re.M) if k == key]


def renderer_cases(script):
    base = tempfile.mkdtemp(prefix="skirnir-priv-")
    try:
        secrets, rw = os.path.join(base, "secrets"), os.path.join(base, "router-rw")
        os.makedirs(secrets, mode=0o700)
        os.makedirs(rw, mode=0o700)
        os.chmod(secrets, 0o700)

        def write(path, text, mode=0o600):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(path, mode)

        def run(conf):
            env = dict(os.environ, RENDER_ENV_CONF=conf, RENDER_ENV_ROUTER_WRITABLE=rw, RENDER_ENV_OWNER_UID=str(os.getuid()),
                       RENDER_ENV_OUT=os.path.join(base, "secrets.env"))
            for k in ("SECRET_STORE_DOMAIN", "SECRET_STORE_CLIENT_ID", "SECRET_STORE_CLIENT_SECRET", "PROJECT_ID", "SECRET_STORE_AUTH_FILE"):
                env.pop(k, None)
            r = subprocess.run(["bash", os.path.join(ROOT, script)], env=env, capture_output=True, text=True, timeout=30)
            return r.returncode, r.stdout + r.stderr

        def marker(name):
            return os.path.join(base, name)

        name = os.path.basename(script)
        auth_rw = os.path.join(rw, "auth.env")
        write(auth_rw, f"touch {marker('auth-rw')}\n")
        good = os.path.join(secrets, "render-env.conf")
        write(good, f"touch {marker('conf-good')}\n")
        rc, out = run(good)
        check(rc == 0 and os.path.exists(marker("conf-good")) and "unvollstaendig" in out,
              f"{name}: a root-only access file is read ({out.strip()[-80:]!r})")

        write(good, f"touch {marker('conf-good2')}\nSECRET_STORE_AUTH_FILE={auth_rw}\n")
        rc, out = run(good)
        check(os.path.exists(marker("conf-good2")) and not os.path.exists(marker("auth-rw")) and "may write" in out,
              f"{name}: an auth file in the router's writable path is not sourced")

        conf_rw = os.path.join(rw, "render-env.conf")
        write(conf_rw, f"touch {marker('conf-rw')}\n")
        rc, out = run(conf_rw)
        check(rc == 0 and not os.path.exists(marker("conf-rw")) and "may write" in out,
              f"{name}: an access file in the router's writable path is not sourced")

        link = os.path.join(secrets, "link.conf")
        os.symlink(conf_rw, link)
        rc, out = run(link)
        check(not os.path.exists(marker("conf-rw")) and "may write" in out, f"{name}: a symlink into the writable path is refused")

        loose = os.path.join(secrets, "loose.conf")
        write(loose, f"touch {marker('conf-loose')}\n", 0o620)
        rc, out = run(loose)
        check(not os.path.exists(marker("conf-loose")) and "group or others" in out, f"{name}: a group-writable access file is refused")

        open_dir = os.path.join(base, "open")
        os.makedirs(open_dir)
        os.chmod(open_dir, 0o777)
        in_open = os.path.join(open_dir, "render-env.conf")
        write(in_open, f"touch {marker('conf-open')}\n")
        rc, out = run(in_open)
        check(not os.path.exists(marker("conf-open")) and "group or others" in out, f"{name}: a file in a world-writable folder is refused")

        rc, out = run(os.path.join(secrets, "missing.conf"))
        check(rc == 0 and "missing" in out, f"{name}: a missing access file renders nothing and exits 0")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def units():
    router = read("router/skirnir-router.service")
    rw = [p for v in unit_values(router, "ReadWritePaths") for p in v.split()]
    check("-/etc/skirnir-router/certs" in [p for v in unit_values(router, "ReadOnlyPaths") for p in v.split()],
          "router unit: certificate folder read-only")
    check("-/etc/skirnir-router-secrets" in [p for v in unit_values(router, "InaccessiblePaths") for p in v.split()],
          "router unit: secret-store folder inaccessible")
    for helper in ("skirnir-router-secrets.service", "skirnir-router-secrets-refresh.service", "skirnir-router-cert.service"):
        u = read(f"router/{helper}")
        check(unit_values(u, "NoNewPrivileges") == ["true"] and unit_values(u, "PrivateTmp") == ["true"]
              and unit_values(u, "ProtectHome") == ["true"], f"{helper}: hardened")

    def under_rw(path):
        return [w for w in rw if path == w or path.startswith(w.rstrip("/") + "/")]

    dests = re.findall(r'\(OPS\["RENDER_ENV_CONF"\], "([^"]+)"', read("deploy/deploy.py"))
    check(len(dests) == 1 and not under_rw(dests[0]), f"deploy.py places render-env.conf outside ReadWritePaths ({dests})")
    for script in ("router/render-env.sh", "monitoring/render-monitoring.sh"):
        m = re.search(r"^CONF=\$\{RENDER_ENV_CONF:-([^}]+)\}", read(script), re.M)
        check(m is not None and not under_rw(m.group(1)) and dests and m.group(1) == dests[0],
              f"{script}: default access file matches deploy.py and lies outside ReadWritePaths")


def passthrough_parity():
    from skirnir_router import common, openai_node
    go = read("agent/passthrough.go")
    block = re.search(r"var passthroughAllowed = map\[string\]\[\]string\{(.*?)\n\}", go, re.S).group(1)
    allowed = set(re.findall(r'"(/[^"]+)":', block))
    sent = set(common.INFER_PATHS) | {"/api/show", "/api/tags", "/api/ps", "/api/version"} | set(openai_node.PATHS) \
        | {openai_node.CHAT_PATH, openai_node.EMBED_PATH, openai_node.MODELS_PATH}
    check(sent <= allowed, f"agent passthrough covers every path the router sends (missing: {sorted(sent - allowed)})")
    check(not (set(common.FORBIDDEN_PATHS) & allowed), "agent passthrough contains no path the router forbids")


if __name__ == "__main__":
    if os.name == "nt":
        print("renderer cases need Linux/bash - only the static checks run here")
    else:
        renderer_cases("router/render-env.sh")
        renderer_cases("monitoring/render-monitoring.sh")
    units()
    passthrough_parity()
    if FAILS:
        print(f"{len(FAILS)} failure(s)")
        sys.exit(1)
    print("selftest_privilege_paths: all passed")
