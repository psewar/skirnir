#!/usr/bin/env python3
"""Deploy the router to its LXC container via the LXC host (SSH root, password from the Secret Store).

Flow: SFTP to <lxc-host>:/tmp/skirnir-router/ -> file push to container (CT_PUSH) -> systemd reload/restart -> smoke test.
Usage:  python deploy.py              (full deploy)
         python deploy.py --status     (status + journal only)
         python deploy.py --pull-roles (fetch CT roles.yaml to the ops folder for viewing/backup)
         python deploy.py --agent [dist-folder] [--version v]   (sign agent binaries and place them on the router)
The LXC host's SSH host key is saved to <ops>/known_hosts on first connection and verified thereafter
(a different key aborts - delete the file if the host was genuinely reinstalled).

All site-specific details (LXC host, CT number, router hostname, secrets folder, production config, CT Secret Store
access) come from the ops folder outside the repo, see ops_env.py. The repo only contains config.example.yaml.
"""
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ops_env  # noqa: E402

ROUTER_DIR = os.path.join(HERE, "..", "router")
OPS = ops_env.load()
CT = OPS["CT"]
secrets_env = None   # set after load_secrets(); decision-embed/deploy_ct.py uses connect()/run()/ct_exec()/ct_push() from here

FILES = [  # (local, target path in container, mode); source is ROUTER_DIR, unless absolute paths (ops folder).
           # If a file with the same name exists in the ops folder under router/ (e.g., skirnir-router-cert.path with the real
           # certificate path), it takes precedence - keeping the repo generic and the site private.
    ("router.py", "/opt/skirnir-router/router.py", "0755"),
    ("ui.html", "/opt/skirnir-router/ui.html", "0644"),
    ("skirnir.png", "/opt/skirnir-router/skirnir.png", "0644"),    # Logo im UI-Kopf
    ("favicon.png", "/opt/skirnir-router/favicon.png", "0644"),    # Tab icon (128 px), also /favicon.ico
    ("decision-tfidf.json", "/etc/skirnir-router/decision-tfidf.json", "0644"),   # Decision Engine tier 1 (train_tfidf.py)
    (OPS["CONFIG_YAML"], "/etc/skirnir-router/config.yaml", "0600"),
    (OPS["RENDER_ENV_CONF"], "/etc/skirnir-router/render-env.conf", "0600"),
    ("skirnir-router.service", "/etc/systemd/system/skirnir-router.service", "0644"),
    ("skirnir-router-cert.path", "/etc/systemd/system/skirnir-router-cert.path", "0644"),
    ("skirnir-router-cert.service", "/etc/systemd/system/skirnir-router-cert.service", "0644"),
    ("render-env.sh", "/opt/skirnir-router/render-env.sh", "0755"),
    ("skirnir-router-secrets.service", "/etc/systemd/system/skirnir-router-secrets.service", "0644"),
    ("skirnir-router-secrets.timer", "/etc/systemd/system/skirnir-router-secrets.timer", "0644"),
    ("skirnir-router-secrets-refresh.service", "/etc/systemd/system/skirnir-router-secrets-refresh.service", "0644"),
]
# router.py is only the entry point; code lives in the router/skirnir_router/ package. Subpackages (e.g., skirnir_router/decision/)
# are included recursively, __pycache__ excluded - a missing subpackage caused the router to hang in a restart loop with ImportError
# after deploy.
PKG_DIR = os.path.join(ROUTER_DIR, "skirnir_router")
PKG_SUBDIRS = []
for _root, _dirs, _files in os.walk(PKG_DIR):
    _dirs[:] = sorted(d for d in _dirs if d != "__pycache__")
    _rel = os.path.relpath(_root, PKG_DIR).replace(os.sep, "/")
    if _rel != ".":
        PKG_SUBDIRS.append(_rel)
    for f in sorted(_files):
        if f.endswith(".py"):
            _sub = "" if _rel == "." else _rel + "/"
            FILES.append((f"skirnir_router/{_sub}{f}", f"/opt/skirnir-router/skirnir_router/{_sub}{f}", "0644"))
_MKDIRS = " ".join(f"/opt/skirnir-router/skirnir_router/{d}" for d in PKG_SUBDIRS)
_TMPDIRS = " ".join(f"/tmp/skirnir-router/skirnir_router/{d}" for d in PKG_SUBDIRS)
# Roles modified in the UI are stored in /etc/skirnir-router/roles.yaml (not in the repo).
# deploy.py leaves this file alone; `--pull-roles` fetches it to the ops folder.


def _local(local):
    if os.path.isabs(local):
        return local
    override = os.path.join(OPS["OPS"], "router", local)
    return override if os.path.isfile(override) else os.path.join(ROUTER_DIR, local)


def _staged(local):
    """Name under /tmp/skirnir-router/ on the LXC host (ops files under their filename)."""
    return os.path.basename(local) if os.path.isabs(local) else local


def ct_host():
    return ops_env.ct_host(OPS)


def run(c, cmd, timeout=120, stdin=None):
    """Command on the LXC host; stdin (text) is passed through - keeping passwords out of the process list."""
    i, o, e = c.exec_command(cmd, timeout=timeout)
    if stdin is not None:
        i.write(stdin)
        i.flush()
        i.channel.shutdown_write()
    out = o.read().decode(errors="replace")
    err = e.read().decode(errors="replace")
    rc = o.channel.recv_exit_status()
    return rc, out, err


def ct_exec(cmd):
    """Shell command in the container (CT_EXEC template from deploy.env)."""
    q = "'" + cmd.replace("'", "'\\''") + "'"
    return OPS["CT_EXEC"].format(ct=CT, cmd=q)


def ct_push(src, dst, mode):
    """File from LXC host to container (CT_PUSH template from deploy.env)."""
    return OPS["CT_PUSH"].format(ct=CT, src=src, dst=dst, mode=mode)


def connect():
    global secrets_env
    secrets_env = ops_env.load_secrets(OPS)
    c = paramiko.SSHClient()
    kh = os.path.join(OPS["OPS"], "known_hosts")
    if os.path.exists(kh):   # Host key pinned: a different key (MITM, reinstalled host) aborts
        c.load_host_keys(kh)
        c.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:                    # first time: accept and remember key (Trust on first use)
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        print("Host key of the LXC host is stored on first connect:", kh)
    c.connect(ct_host(), username=OPS["HOST_USER"], password=os.environ[OPS["HOST_PASS_ENV"]], timeout=20)
    if not os.path.exists(kh):
        c.save_host_keys(kh)
    return c


def curl_auth(url_and_args):
    """curl with Basic Auth from stdin (`-K -`), so the UI password doesn't appear in the process list of host and container."""
    return "curl -s -m 8 -K - " + url_and_args, 'user = "%s:%s"\n' % (OPS["UI_USER"], os.environ.get(OPS["UI_PASS_ENV"], ""))


AGENT_BINARIES = [  # (filename in dist/, os, arch) - names as in the GitHub release
    ("skirnir-agent.exe", "windows", "amd64"),
    ("skirnir-agent-linux-amd64", "linux", "amd64"),
]


def agent_signing_key():
    """Operator key for agent manifests: Ed25519, raw (32-byte seed, base64) in the ops folder. Generated on first call;
    the public part belongs in the agent configuration (update.public_key) and optionally in modes.agent_update.public_key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = os.path.join(ops_env.ops_dir(), "agent-update.key")
    if os.path.exists(path):
        seed = base64.b64decode(open(path, encoding="utf-8").read().strip())
        key = Ed25519PrivateKey.from_private_bytes(seed)
    else:
        key = Ed25519PrivateKey.generate()
        seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        with open(path, "w", encoding="utf-8") as f:
            f.write(base64.b64encode(seed).decode())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print("New operator key for agent updates:", path)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(pub).decode()


def check_authenticode(files):
    """Windows binaries must be signed with the operator certificate (AGENT_SIGN_THUMBPRINT in deploy.env; agent/sign.ps1),
    otherwise Smart App Control / WDAC on the nodes blocks the new file (observed 2026-09-26). Without thumbprint, only a warning."""
    want = (OPS.get("AGENT_SIGN_THUMBPRINT") or "").upper().replace(" ", "").replace(":", "")
    if sys.platform != "win32":
        if want:
            print("Note: signature check only possible on Windows (Get-AuthenticodeSignature) - not checked on Linux")
        return
    for f in files:
        if f["os"] != "windows":
            continue
        ps = ("$s = Get-AuthenticodeSignature -LiteralPath '" + f["_src"].replace("'", "''") + "'; "
              "Write-Output ($s.Status.ToString() + '|' + $(if ($s.SignerCertificate) { $s.SignerCertificate.Thumbprint + '|' + $s.SignerCertificate.Subject } else { '|' }))")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True)
        status, thumb, subject = (r.stdout.strip().split("|", 2) + ["", ""])[:3]
        if want:
            if status != "Valid" or thumb.upper() != want:
                print(f"Aborted: {os.path.basename(f['_src'])} is not signed with the operator certificate (status {status or '?'}, signer {subject or '-'}). "
                      "Run agent/sign.ps1 first."); sys.exit(2)
            print(f"Signature ok: {os.path.basename(f['_src'])} by {subject}")
        elif status != "Valid":
            print(f"WARNING: {os.path.basename(f['_src'])} is unsigned ({status or '?'}) - Smart App Control on the nodes blocks it (AGENT_SIGN_THUMBPRINT in deploy.env + agent/sign.ps1)")


def deploy_agent(c, dist_dir=None):
    """Sign binaries from dist/ (manifest) and place them in /etc/skirnir-router/agent/. Version from `<exe> version`."""
    dist_dir = dist_dir or os.path.join(os.path.dirname(ROUTER_DIR), "agent", "dist")
    files = []
    version = None
    for name, os_name, arch in AGENT_BINARIES:
        path = os.path.join(dist_dir, name)
        if not os.path.exists(path):
            print("missing, skipped:", path)
            continue
        if os_name == "windows" and sys.platform == "win32":
            try:
                v = subprocess.run([path, "version"], capture_output=True, text=True).stdout.strip()
            except OSError as e:   # Application Control blocks a freshly built, unsigned Exe (2026-09-26) -> --version <v>
                print(f"Note: {name} cannot be started here ({e.__class__.__name__}) - version from --version. "
                      "If Smart App Control blocks the new file, the canary rejects it at first as well; the router then retries "
                      "every 30 min until the cloud verdict changes (or the file is signed, agent/sign.ps1)")
                v = None
            if v and version and v != version:
                print(f"Aborted: versions differ ({version} vs {v})"); sys.exit(2)
            version = v or version
        data = open(path, "rb").read()
        files.append({"name": f"skirnir-agent-{{v}}-{os_name}-{arch}" + (".exe" if os_name == "windows" else ""),
                      "os": os_name, "arch": arch, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "_src": path})
    version = version or (sys.argv[sys.argv.index("--version") + 1] if "--version" in sys.argv else None)
    if not files or not version:
        print("Aborted: no binaries or no version (on Linux pass --version <v>)"); sys.exit(2)
    check_authenticode(files)
    for f in files:
        f["name"] = f["name"].replace("{v}", version)
    key, pub = agent_signing_key()
    manifest = {"version": version, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": [{k: v for k, v in f.items() if k != "_src"} for f in files]}
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = base64.b64encode(key.sign(canonical)).decode()
    mpath = os.path.join(dist_dir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump({"manifest": manifest, "signature": signature}, fh, indent=1, ensure_ascii=False)
    run(c, "mkdir -p /tmp/skirnir-agent")
    sftp = c.open_sftp()
    for f in files:
        sftp.put(f["_src"], f"/tmp/skirnir-agent/{f['name']}")
    sftp.put(mpath, "/tmp/skirnir-agent/manifest.json")
    sftp.close()
    run(c, ct_exec("mkdir -p /etc/skirnir-router/agent"))
    for f in files:
        rc, out, err = run(c, ct_push(f"/tmp/skirnir-agent/{f['name']}", f"/etc/skirnir-router/agent/{f['name']}", "0644"))
        if rc != 0:
            print("push failed:", f["name"], out, err); sys.exit(1)
    rc, out, err = run(c, ct_push("/tmp/skirnir-agent/manifest.json", "/etc/skirnir-router/agent/manifest.json", "0644"))
    run(c, "rm -rf /tmp/skirnir-agent")
    rc, out, err = run(c, ct_exec("ls -la /etc/skirnir-router/agent/ && python3 -c \"import json; m=json.load(open('/etc/skirnir-router/agent/manifest.json'))['manifest']; print('Manifest', m['version'], [f['name'] for f in m['files']])\""))
    print(out.strip(), err.strip())
    print(f"Agent {version} stored ({len(files)} file(s)). Public key for the agents' update.public_key: {pub}")


def _venv_command():
    """Shell command (in CT) that creates or updates /opt/skirnir-router/venv and ends with 'venv ok <asyncpg-version>'."""
    specs = [line.strip() for line in open(os.path.join(ROUTER_DIR, "requirements-session.txt"), encoding="utf-8")
             if line.strip() and not line.lstrip().startswith(("#", "-"))]
    venv = "/opt/skirnir-router/venv"
    # Refresh package lists only when installation is actually needed (stale lists point to remote .deb versions)
    return ("set -e; dpkg -s python3-venv >/dev/null 2>&1 || { apt-get update -q >/dev/null && apt-get install -y -q python3-venv >/dev/null; }; "
            f"[ -x {venv}/bin/python ] || python3 -m venv --system-site-packages {venv}; "
            f"{venv}/bin/python -m pip install -q --disable-pip-version-check " + " ".join(shlex.quote(s) for s in specs) + "; "
            f"{venv}/bin/python -c 'import aiohttp, yaml, asyncpg; print(\"venv ok\", asyncpg.__version__)'")


def _router_version():
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', open(os.path.join(ROUTER_DIR, "skirnir_router", "common.py"), encoding="utf-8").read(), re.M)
    return m.group(1) if m else "?"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # systemctl output contains Unicode dots, Windows console is cp1252
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return
    host = OPS["ROUTER_HOST"]
    c = connect()
    if "--agent" in sys.argv:
        deploy_agent(c, sys.argv[sys.argv.index("--agent") + 1] if len(sys.argv) > sys.argv.index("--agent") + 1 and not sys.argv[sys.argv.index("--agent") + 1].startswith("--") else None)
        c.close()
        return
    status_only = "--status" in sys.argv
    if "--pull-roles" in sys.argv:
        rc, out, err = run(c, ct_exec("cat /etc/skirnir-router/roles.yaml 2>/dev/null || echo '# (no roles.yaml present)'"))
        open(OPS["ROLES_YAML"], "w", encoding="utf-8").write(out)
        print("roles.yaml fetched:", len(out), "bytes ->", OPS["ROLES_YAML"])
        c.close()
        return
    if not status_only:
        # Tier 6: validate local config against the schema (ignoring potentially stale local roles.yaml) - fail-closed
        chk = subprocess.run([sys.executable, os.path.join(ROUTER_DIR, "router.py"), "--check", OPS["CONFIG_YAML"], "--pure"],
                             capture_output=True, text=True)
        print("Local schema check:", (chk.stdout + chk.stderr).strip())
        if chk.returncode != 0:
            print("Aborted: config.yaml invalid, nothing rolled out")
            sys.exit(2)
        # Deploy manifest (Supply Chain): timestamp, router version, and SHA-256 of each deployed file -> /admin/state build
        # (no workspace path, no username: the manifest resides on the router and appears in the UI)
        manifest = {"deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "router_version": _router_version(),
                    "files": {_staged(local): hashlib.sha256(open(_local(local), "rb").read()).hexdigest() for local, _, _ in FILES}}
        with open(os.path.join(ROUTER_DIR, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1)
        FILES.append(("manifest.json", "/etc/skirnir-router/manifest.json", "0644"))
        # Provide router Python BEFORE file swap (fail-closed: if it fails, the old router continues running unchanged).
        # venv with system packages (apt: aiohttp, yaml, cryptography) plus pip dependencies of optional layers from
        # router/requirements-session.txt - Debian 12 offers asyncpg only in 0.27, required is >= 0.29.
        rc, out, err = run(c, ct_exec(_venv_command()), timeout=600)
        print(out.strip().splitlines()[-1] if out.strip() else err.strip()[-300:])
        if rc != 0 or "venv ok" not in out:
            print("Aborted: router venv not ready - nothing rolled out, the old router keeps running")
            c.close()
            sys.exit(1)
        # Rename 2026-09-25 (ollama-router -> skirnir-router): one-time migration on the CT before new files arrive.
        rc, out, err = run(c, ct_exec(
            "if [ ! -d /etc/skirnir-router ] && [ -d /etc/ollama-router ]; then "
            "systemctl disable --now ollama-router ollama-router-cert.path ollama-router-secrets.timer >/dev/null 2>&1; "
            "rm -f /etc/systemd/system/ollama-router.service /etc/systemd/system/ollama-router-cert.path /etc/systemd/system/ollama-router-cert.service "
            "/etc/systemd/system/ollama-router-secrets.service /etc/systemd/system/ollama-router-secrets.timer /etc/systemd/system/ollama-router-secrets-refresh.service; "
            "mv /etc/ollama-router /etc/skirnir-router; [ -d /opt/ollama-router ] && mv /opt/ollama-router /opt/skirnir-router; "
            "[ -d /var/log/ollama-router ] && mv /var/log/ollama-router /var/log/skirnir-router; [ -d /var/lib/ollama-router ] && mv /var/lib/ollama-router /var/lib/skirnir-router; "
            "systemctl daemon-reload; echo 'moved: ollama-router -> skirnir-router'; fi"))
        if out.strip():
            print(out.strip())
        run(c, f"mkdir -p /tmp/skirnir-router/skirnir_router {_TMPDIRS}")
        sftp = c.open_sftp()
        for local, _remote, _ in FILES:
            sftp.put(_local(local), f"/tmp/skirnir-router/{_staged(local)}")
        sftp.close()
        rc, out, err = run(c, ct_exec(f"mkdir -p /opt/skirnir-router/skirnir_router /etc/skirnir-router {_MKDIRS}"))
        for local, remote, mode in FILES:
            rc, out, err = run(c, ct_push(f"/tmp/skirnir-router/{_staged(local)}", remote, mode))
            if rc != 0:
                print("push failed:", local, out, err)
                sys.exit(1)
        run(c, "rm -rf /tmp/skirnir-router")
        rc, out, err = run(c, ct_exec("python3 /opt/skirnir-router/router.py --check /etc/skirnir-router/config.yaml && "
                                  "python3 -c \"import yaml; yaml.safe_load(open(\\\"/etc/skirnir-router/config.yaml\\\"))\""))
        print(out.strip(), err.strip())
        if rc != 0 or "configuration ok" not in out:
            print("Aborted: schema check on the CT failed - service NOT restarted (the old process keeps running, but the files are already replaced)")
            c.close()
            sys.exit(1)
        # Restart only AFTER successful check as a separate command (Review 2026-09-25: previously it hung via ';' behind the
        # '||' branch of the same command chain and ran even if the check failed)
        rc, out, err = run(c, ct_exec("dpkg -s python3-cryptography >/dev/null 2>&1 || (apt-get install -y -q python3-cryptography >/dev/null 2>&1 && echo cryptography-installed); "
                                  "systemctl daemon-reload && systemctl enable skirnir-router >/dev/null 2>&1; systemctl enable --now skirnir-router-cert.path >/dev/null 2>&1; "
                                  "systemctl enable --now skirnir-router-secrets.timer >/dev/null 2>&1; /opt/skirnir-router/render-env.sh; systemctl restart --no-block skirnir-router; sleep 3; echo restarted"))
        print(out.strip(), err.strip())
        time.sleep(3)
    state_cmd, state_stdin = curl_auth(f"--resolve {host}:11435:127.0.0.1 https://{host}:11435/admin/state | head -c 400")
    rc, out, err = run(c, ct_exec("systemctl --no-pager --lines=0 status skirnir-router | head -5; "
                              "echo ---; journalctl -u skirnir-router --no-pager -n 25 -o short; "
                              f"echo ---; curl -s -m 5 --resolve {host}:11434:127.0.0.1 https://{host}:11434/api/version; echo; "   # /api/version is exempt from client auth (tier 2); /api/tags required a token in enforce mode
                              f"echo ---; {state_cmd}; echo; "
                              f"echo ---; systemctl is-active skirnir-router-cert.path; openssl s_client -connect 127.0.0.1:11435 -servername {host} </dev/null 2>/dev/null | openssl x509 -noout -subject -enddate"),
                      stdin=state_stdin)
    print(out)
    if err.strip():
        print("STDERR:", err)
    c.close()


if __name__ == "__main__":
    main()
