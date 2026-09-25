#!/usr/bin/env python3
"""Deploy des Routers in seinen LXC-Container ueber den LXC-Host (SSH root, Passwort aus dem Secret-Store).

Ablauf: SFTP nach <lxc-host>:/tmp/ollama-router/ -> Datei-Push in den Container (CT_PUSH) -> systemd reload/restart -> Smoke-Test.
Aufruf:  python deploy.py              (voller Deploy)
         python deploy.py --status     (nur Status + Journal)
         python deploy.py --pull-roles (roles.yaml des CT in den Ops-Ordner holen, zur Ansicht/Sicherung)
         python deploy.py --agent [dist-ordner] [--version v]   (Agent-Binaries signieren und auf den Router legen)
Der SSH-Host-Schluessel des LXC-Hosts wird beim ersten Verbinden in <ops>/known_hosts gespeichert und danach geprueft
(ein anderer Schluessel bricht ab - Datei loeschen, wenn der Host wirklich neu aufgesetzt wurde).

Alles Standortspezifische (LXC-Host, CT-Nummer, Router-Hostname, Secrets-Ordner, Produktivkonfiguration, Secret-Store-
Zugang des CT) kommt aus dem Ops-Ordner ausserhalb des Repos, siehe ops_env.py. Das Repo enthaelt nur config.example.yaml.
"""
import base64
import hashlib
import json
import os
import re
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
secrets_env = None   # nach load_secrets() gesetzt; decision-embed/deploy_ct.py nutzt connect()/run()/ct_exec()/ct_push() von hier

FILES = [  # (lokal, Zielpfad im Container, mode); Quelle ist ROUTER_DIR, ausser bei absoluten Pfaden (Ops-Ordner).
           # Liegt im Ops-Ordner unter router/ eine Datei gleichen Namens (z. B. ollama-router-cert.path mit dem echten
           # Zertifikatspfad), gewinnt sie - so bleibt das Repo generisch und der Standort privat.
    ("router.py", "/opt/ollama-router/router.py", "0755"),
    ("ui.html", "/opt/ollama-router/ui.html", "0644"),
    ("skirnir.png", "/opt/ollama-router/skirnir.png", "0644"),    # Logo im UI-Kopf
    ("favicon.png", "/opt/ollama-router/favicon.png", "0644"),    # Tab-Icon (128 px), auch /favicon.ico
    ("decision-tfidf.json", "/etc/ollama-router/decision-tfidf.json", "0644"),   # Decision Engine Stufe 1 (train_tfidf.py)
    (OPS["CONFIG_YAML"], "/etc/ollama-router/config.yaml", "0600"),
    (OPS["RENDER_ENV_CONF"], "/etc/ollama-router/render-env.conf", "0600"),
    ("ollama-router.service", "/etc/systemd/system/ollama-router.service", "0644"),
    ("ollama-router-cert.path", "/etc/systemd/system/ollama-router-cert.path", "0644"),
    ("ollama-router-cert.service", "/etc/systemd/system/ollama-router-cert.service", "0644"),
    ("render-env.sh", "/opt/ollama-router/render-env.sh", "0755"),
    ("ollama-router-secrets.service", "/etc/systemd/system/ollama-router-secrets.service", "0644"),
    ("ollama-router-secrets.timer", "/etc/systemd/system/ollama-router-secrets.timer", "0644"),
    ("ollama-router-secrets-refresh.service", "/etc/systemd/system/ollama-router-secrets-refresh.service", "0644"),
]
# router.py ist nur der Einstieg; der Code liegt im Paket router/ollama_router/. Unterpakete (z. B. ollama_router/decision/)
# werden rekursiv mitgenommen, __pycache__ nicht - ein fehlendes Unterpaket liess den Router nach dem Deploy mit ImportError
# im Neustart-Kreis haengen.
PKG_DIR = os.path.join(ROUTER_DIR, "ollama_router")
PKG_SUBDIRS = []
for _root, _dirs, _files in os.walk(PKG_DIR):
    _dirs[:] = sorted(d for d in _dirs if d != "__pycache__")
    _rel = os.path.relpath(_root, PKG_DIR).replace(os.sep, "/")
    if _rel != ".":
        PKG_SUBDIRS.append(_rel)
    for f in sorted(_files):
        if f.endswith(".py"):
            _sub = "" if _rel == "." else _rel + "/"
            FILES.append((f"ollama_router/{_sub}{f}", f"/opt/ollama-router/ollama_router/{_sub}{f}", "0644"))
_MKDIRS = " ".join(f"/opt/ollama-router/ollama_router/{d}" for d in PKG_SUBDIRS)
_TMPDIRS = " ".join(f"/tmp/ollama-router/ollama_router/{d}" for d in PKG_SUBDIRS)
# Rollen, die in der UI geaendert werden, liegen in /etc/ollama-router/roles.yaml (nicht im Repo).
# deploy.py laesst diese Datei in Ruhe; `--pull-roles` holt sie in den Ops-Ordner.


def _local(local):
    if os.path.isabs(local):
        return local
    override = os.path.join(OPS["OPS"], "router", local)
    return override if os.path.isfile(override) else os.path.join(ROUTER_DIR, local)


def _staged(local):
    """Name unter /tmp/ollama-router/ auf dem LXC-Host (Ops-Dateien unter ihrem Dateinamen)."""
    return os.path.basename(local) if os.path.isabs(local) else local


def ct_host():
    return ops_env.ct_host(OPS)


def run(c, cmd, timeout=120, stdin=None):
    """Kommando auf dem LXC-Host; stdin (Text) wird durchgereicht - so bleiben Passwoerter aus der Prozessliste."""
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
    """Shell-Kommando im Container (Vorlage CT_EXEC aus deploy.env)."""
    q = "'" + cmd.replace("'", "'\\''") + "'"
    return OPS["CT_EXEC"].format(ct=CT, cmd=q)


def ct_push(src, dst, mode):
    """Datei vom LXC-Host in den Container (Vorlage CT_PUSH aus deploy.env)."""
    return OPS["CT_PUSH"].format(ct=CT, src=src, dst=dst, mode=mode)


def connect():
    global secrets_env
    secrets_env = ops_env.load_secrets(OPS)
    c = paramiko.SSHClient()
    kh = os.path.join(OPS["OPS"], "known_hosts")
    if os.path.exists(kh):   # Host-Schluessel gepinnt: ein anderer Schluessel (MITM, neu aufgesetzter Host) bricht ab
        c.load_host_keys(kh)
        c.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:                    # erstes Mal: Schluessel uebernehmen und merken (Trust on first use)
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        print("Host-Schluessel des LXC-Hosts wird beim ersten Verbinden gespeichert:", kh)
    c.connect(ct_host(), username=OPS["HOST_USER"], password=os.environ[OPS["HOST_PASS_ENV"]], timeout=20)
    if not os.path.exists(kh):
        c.save_host_keys(kh)
    return c


def curl_auth(url_and_args):
    """curl mit Basic Auth aus stdin (`-K -`), damit das UI-Passwort nicht in der Prozessliste von Host und Container steht."""
    return "curl -s -m 8 -K - " + url_and_args, 'user = "%s:%s"\n' % (OPS["UI_USER"], os.environ.get(OPS["UI_PASS_ENV"], ""))


AGENT_BINARIES = [  # (Dateiname in dist/, os, arch) - Namen wie im GitHub-Release
    ("ollama-router-agent.exe", "windows", "amd64"),
    ("ollama-router-agent-linux-amd64", "linux", "amd64"),
]


def agent_signing_key():
    """Betreiber-Schluessel fuer Agent-Manifeste: Ed25519, roh (32 Bytes Seed, base64) im Ops-Ordner. Beim ersten Aufruf erzeugt;
    der oeffentliche Teil gehoert in die Agent-Konfiguration (update.public_key) und optional in modes.agent_update.public_key."""
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
        print("Neuer Betreiber-Schluessel fuer Agent-Updates:", path)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(pub).decode()


def deploy_agent(c, dist_dir=None):
    """Binaries aus dist/ signieren (Manifest) und nach /etc/ollama-router/agent/ legen. Version aus `<exe> version`."""
    dist_dir = dist_dir or os.path.join(os.path.dirname(ROUTER_DIR), "agent-go", "dist")
    files = []
    version = None
    for name, os_name, arch in AGENT_BINARIES:
        path = os.path.join(dist_dir, name)
        if not os.path.exists(path):
            print("fehlt, uebersprungen:", path)
            continue
        if os_name == "windows" and sys.platform == "win32":
            v = subprocess.run([path, "version"], capture_output=True, text=True).stdout.strip()
            if version and v != version:
                print(f"Abbruch: Versionen unterschiedlich ({version} vs {v})"); sys.exit(2)
            version = v
        data = open(path, "rb").read()
        files.append({"name": f"ollama-router-agent-{{v}}-{os_name}-{arch}" + (".exe" if os_name == "windows" else ""),
                      "os": os_name, "arch": arch, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "_src": path})
    version = version or (sys.argv[sys.argv.index("--version") + 1] if "--version" in sys.argv else None)
    if not files or not version:
        print("Abbruch: keine Binaries oder keine Version (unter Linux --version <v> angeben)"); sys.exit(2)
    for f in files:
        f["name"] = f["name"].replace("{v}", version)
    key, pub = agent_signing_key()
    manifest = {"version": version, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": [{k: v for k, v in f.items() if k != "_src"} for f in files]}
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = base64.b64encode(key.sign(canonical)).decode()
    mpath = os.path.join(dist_dir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump({"manifest": manifest, "signature": signature}, fh, indent=1, ensure_ascii=False)
    run(c, "mkdir -p /tmp/ollama-router-agent")
    sftp = c.open_sftp()
    for f in files:
        sftp.put(f["_src"], f"/tmp/ollama-router-agent/{f['name']}")
    sftp.put(mpath, "/tmp/ollama-router-agent/manifest.json")
    sftp.close()
    run(c, ct_exec("mkdir -p /etc/ollama-router/agent"))
    for f in files:
        rc, out, err = run(c, ct_push(f"/tmp/ollama-router-agent/{f['name']}", f"/etc/ollama-router/agent/{f['name']}", "0644"))
        if rc != 0:
            print("push failed:", f["name"], out, err); sys.exit(1)
    rc, out, err = run(c, ct_push("/tmp/ollama-router-agent/manifest.json", "/etc/ollama-router/agent/manifest.json", "0644"))
    run(c, "rm -rf /tmp/ollama-router-agent")
    rc, out, err = run(c, ct_exec("ls -la /etc/ollama-router/agent/ && python3 -c \"import json; m=json.load(open('/etc/ollama-router/agent/manifest.json'))['manifest']; print('Manifest', m['version'], [f['name'] for f in m['files']])\""))
    print(out.strip(), err.strip())
    print(f"Agent {version} hinterlegt ({len(files)} Datei(en)). Oeffentlicher Schluessel fuer update.public_key der Agenten: {pub}")


def _router_version():
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', open(os.path.join(ROUTER_DIR, "ollama_router", "common.py"), encoding="utf-8").read(), re.M)
    return m.group(1) if m else "?"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # systemctl-Ausgabe enthaelt Unicode-Punkte, Windows-Konsole ist cp1252
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
        rc, out, err = run(c, ct_exec("cat /etc/ollama-router/roles.yaml 2>/dev/null || echo '# (keine roles.yaml vorhanden)'"))
        open(OPS["ROLES_YAML"], "w", encoding="utf-8").write(out)
        print("roles.yaml geholt:", len(out), "Bytes ->", OPS["ROLES_YAML"])
        c.close()
        return
    if not status_only:
        # Stufe 6: Konfiguration lokal gegen das Schema pruefen (ohne die evtl. veraltete lokale roles.yaml) - fail-closed
        chk = subprocess.run([sys.executable, os.path.join(ROUTER_DIR, "router.py"), "--check", OPS["CONFIG_YAML"], "--pure"],
                             capture_output=True, text=True)
        print("Schema-Check lokal:", (chk.stdout + chk.stderr).strip())
        if chk.returncode != 0:
            print("Abbruch: config.yaml ungueltig, nichts ausgerollt")
            sys.exit(2)
        # Deploy-Manifest (Supply Chain): Zeitpunkt, Router-Version und SHA-256 jeder ausgerollten Datei -> /admin/state build
        # (kein Arbeitsplatz-Pfad, kein Benutzername: das Manifest liegt auf dem Router und erscheint in der UI)
        manifest = {"deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "router_version": _router_version(),
                    "files": {_staged(local): hashlib.sha256(open(_local(local), "rb").read()).hexdigest() for local, _, _ in FILES}}
        with open(os.path.join(ROUTER_DIR, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1)
        FILES.append(("manifest.json", "/etc/ollama-router/manifest.json", "0644"))
        run(c, f"mkdir -p /tmp/ollama-router/ollama_router {_TMPDIRS}")
        sftp = c.open_sftp()
        for local, _remote, _ in FILES:
            sftp.put(_local(local), f"/tmp/ollama-router/{_staged(local)}")
        sftp.close()
        rc, out, err = run(c, ct_exec(f"mkdir -p /opt/ollama-router/ollama_router /etc/ollama-router {_MKDIRS}"))
        for local, remote, mode in FILES:
            rc, out, err = run(c, ct_push(f"/tmp/ollama-router/{_staged(local)}", remote, mode))
            if rc != 0:
                print("push failed:", local, out, err)
                sys.exit(1)
        run(c, "rm -rf /tmp/ollama-router")
        rc, out, err = run(c, ct_exec("python3 /opt/ollama-router/router.py --check /etc/ollama-router/config.yaml && "
                                  "python3 -c \"import yaml; yaml.safe_load(open(\\\"/etc/ollama-router/config.yaml\\\"))\""))
        print(out.strip(), err.strip())
        if rc != 0 or "Konfiguration ok" not in out:
            print("Abbruch: Schema-Check auf dem CT fehlgeschlagen - Dienst NICHT neu gestartet (alter Prozess laeuft weiter, Dateien sind aber schon ersetzt)")
            c.close()
            sys.exit(1)
        # Neustart erst NACH bestandenem Check als eigenes Kommando (Review 2026-09-25: vorher hing er per ';' hinter dem
        # '||'-Zweig derselben Kommandokette und lief auch bei gescheitertem Check)
        rc, out, err = run(c, ct_exec("dpkg -s python3-cryptography >/dev/null 2>&1 || (apt-get install -y -q python3-cryptography >/dev/null 2>&1 && echo cryptography-installiert); "
                                  "systemctl daemon-reload && systemctl enable ollama-router >/dev/null 2>&1; systemctl enable --now ollama-router-cert.path >/dev/null 2>&1; "
                                  "systemctl enable --now ollama-router-secrets.timer >/dev/null 2>&1; /opt/ollama-router/render-env.sh; systemctl restart --no-block ollama-router; sleep 3; echo restarted"))
        print(out.strip(), err.strip())
        time.sleep(3)
    state_cmd, state_stdin = curl_auth(f"--resolve {host}:11435:127.0.0.1 https://{host}:11435/admin/state | head -c 400")
    rc, out, err = run(c, ct_exec("systemctl --no-pager --lines=0 status ollama-router | head -5; "
                              "echo ---; journalctl -u ollama-router --no-pager -n 25 -o short; "
                              f"echo ---; curl -s -m 5 --resolve {host}:11434:127.0.0.1 https://{host}:11434/api/version; echo; "   # /api/version ist von der Client-Auth ausgenommen (Stufe 2); /api/tags braeuchte im enforce-Modus ein Token
                              f"echo ---; {state_cmd}; echo; "
                              f"echo ---; systemctl is-active ollama-router-cert.path; openssl s_client -connect 127.0.0.1:11435 -servername {host} </dev/null 2>/dev/null | openssl x509 -noout -subject -enddate"),
                      stdin=state_stdin)
    print(out)
    if err.strip():
        print("STDERR:", err)
    c.close()


if __name__ == "__main__":
    main()
