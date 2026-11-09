# Installing the router

**English** · [Deutsch](install-router.de.md) · [README](../README.md)

The router is one Python process with a systemd unit. This page covers a fresh installation on Debian or Ubuntu, the
TLS certificate, the firewall, secrets, the optional decision engine and the command-line tools. GPU nodes are in
[install-agent.md](install-agent.md), the configuration in [configuration.md](configuration.md).

## Requirements

| What | Details |
|---|---|
| Operating system | Debian 12 or Ubuntu (or comparable; `install.sh` supports Debian and Ubuntu, elsewhere follow [Manual installation](#manual-installation)). A container is fine. On Windows the router runs with the default event loop (development). |
| Python | 3.11 or newer |
| Packages | `python3`, `python3-venv`, `python3-aiohttp`, `python3-yaml`, `python3-cryptography`, `python3-paho-mqtt`, `python3-uvloop`, `curl`, `jq` (`install.sh` installs missing ones). What the distribution lacks or ships too old (on Debian 12: `asyncpg` for the session store) comes into the venv only from the hash-locked `router/requirements-lock.txt` (ADR-0074). |
| Ports | **11434** for clients (Ollama, OpenAI and Anthropic APIs), **11435** for the agents' tunnel, the web UI, `/admin/*` and `/metrics` |
| Name and certificate | A DNS name that clients and nodes use (`<name>`, e.g. `router.example.net`) and a TLS certificate for it, see [TLS certificate](#tls-certificate) |

## Install with install.sh

From the `router/` folder of a clone or release ([ADR-0069](adr/0069-router-install-script.md)):

```bash
sudo bash router/install.sh            # install or upgrade
sudo bash router/install.sh --check    # only report what is missing or wrong, change nothing
```

Before changing anything the script checks: root, Debian/Ubuntu, Python 3.11 or newer, a complete source folder. Then it

1. installs missing packages with `apt`,
2. replaces the program files in `/opt/skirnir-router` (`router.py`, the package `skirnir_router/`, `ui.html`, the example
   configurations, `decision-tfidf.json`, `render-env.sh` …),
3. creates the venv `/opt/skirnir-router/venv` (`--system-site-packages`); `skirnir_router/pydeps.py` lists the
   requirements of `requirements-session.txt` the distribution does not meet, and pip installs exactly those from
   `requirements-lock.txt` with `--require-hashes --no-deps` - pinned versions, every artifact checked against its
   SHA-256; an unmet requirement without a lock entry stops the install (ADR-0074),
4. copies the systemd units to `/etc/systemd/system/` and runs `systemctl daemon-reload`,
5. never touches the configuration: without `/etc/skirnir-router/config.yaml` it prints the `--init` command as the next step;
   with one it checks it as the service will read it (`router.py --check --files` without capabilities, then `--posture`)
   and tells you to start or restart the service.

The last line is always `STATE: installed`, `STATE: checked` or `STATE: refused` (with the reason above it).
**Upgrade** = run the script again from the new release, then `systemctl restart skirnir-router`. Options: `--prefix DIR`
(default `/opt/skirnir-router`), `--config-dir DIR` (default `/etc/skirnir-router`); `--root DIR`, `--no-venv` and
`--os-release FILE` are for packaging and tests (`--help` lists them).

## First configuration

Easiest ([ADR-0067](adr/0067-guided-first-configuration.md)):

```bash
sudo python3 /opt/skirnir-router/router.py --init /etc/skirnir-router --public-url https://<name>:11435
```

`--init` writes `/etc/skirnir-router/config.yaml` from [router/config.minimal.yaml](../router/config.minimal.yaml):

- asks the password for the web UI and admin API without echo (twice on a terminal, at least 12 characters) and stores only
  its PBKDF2 hash under `control_auth.users` (user `admin`, or `--user <name>`),
- generates one client token (`--client <name>`, default `my-client`), shows it **once** and stores only its sha256,
- sets `client_auth.mode: enforce`, `locked: true`, `api_tls: true` and the role `standard` with `qwen3:8b`,
- takes the certificate paths from `--cert` and `--key` (default `/etc/skirnir-router/certs/fullchain.pem` and `privkey.pem`),
- creates the file owner-only (0600) and refuses if it already exists, then runs the same checks as `--check` and `--posture`.
  The last line is `INIT: written` or `INIT: refused`.

By hand instead: copy [router/config.example.yaml](../router/config.example.yaml) (the full reference) or
`config.minimal.yaml` to `/etc/skirnir-router/config.yaml` (mode 0600) and adapt `public_url`, certificate paths, roles,
catalogue and clients. Password hashes for the UI come from `python3 router.py --hash` (asks without echo); a client hash is
the sha256 of its token:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'    # a new token
printf '%s' '<token>' | sha256sum                                # its token_sha256
```

What each section means: [configuration.md](configuration.md).

## TLS certificate

The control port uses `router.control_tls` (`cert`, `key`); with `router.api_tls: true` the API port uses the same
certificate. `api_tls` defaults to `false` and a control port without `control_tls` speaks plain HTTP, so set both
(`config.minimal.yaml` and `--init` do).

Two ways:

- **A public certificate**, e.g. from Let's Encrypt. If the router is not reachable from the internet, use the DNS-01
  challenge (an ACME client with a DNS provider plugin). Agents, Home Assistant and other clients trust it without further
  steps.
- **A private CA.** Every agent then needs `router.ca_file: <pem>` (Linux `setup --ca-file`, see
  [install-agent.md](install-agent.md#trusting-the-routers-certificate)), and every client must trust the CA too (Home
  Assistant, scripts, browsers).

The service runs as root **without capabilities** ([ADR-0044](adr/0044-systemd-hardening-root-kept-for-now.md)): root then
reads only files whose owner permissions allow it, like any other user. Keep the key owned by root with mode 0600 (or 0640)
and the folders on the path traversable. A key readable only through a group or another user fails at start. Check it the way
the service reads it:

```bash
sudo setpriv --inh-caps=-all --bounding-set=-all /opt/skirnir-router/venv/bin/python \
  /opt/skirnir-router/router.py --check /etc/skirnir-router/config.yaml --files
```

`skirnir-router-cert.path` restarts the router when the certificate file changes. It watches
`/etc/skirnir-router/certs/fullchain.pem`, the path `router.py --init` and both example configurations use; if your
`control_tls.cert` is elsewhere, adjust `PathChanged=` in `/etc/systemd/system/skirnir-router-cert.path`. Enable it with
`systemctl daemon-reload && systemctl enable --now skirnir-router-cert.path`.

## Start the service

```bash
sudo systemctl enable --now skirnir-router
journalctl -u skirnir-router -f
```

The start log lists the posture findings (lines `posture …`). Then open `https://<name>:11435/` and log in.

| Unit | Purpose |
|---|---|
| `skirnir-router.service` | The router. Runs `/opt/skirnir-router/venv/bin/python router.py /etc/skirnir-router/config.yaml` as root without capabilities, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=tmpfs`, `PrivateTmp`, `UMask=0077`. Writes only `/etc/skirnir-router`, its state directory `/var/lib/skirnir-router` and its log directory `/var/log/skirnir-router`. Reads `secrets.env` as environment. |
| `skirnir-router-cert.path` + `.service` | Optional: restart on a renewed certificate (see above). |
| `skirnir-router-secrets.service` | Renders `secrets.env` before the router starts (`render-env.sh`); does nothing without `render-env.conf`. |
| `skirnir-router-secrets.timer` + `-secrets-refresh.service` | Optional: renders daily and restarts the router when a secret changed. |

## Firewall

- **11435**: only from the GPU nodes (agent tunnel) and admin machines (web UI, Prometheus). The agents connect outbound to
  it; nodes need no inbound rule.
- **11434**: from the clients (Home Assistant, Node-RED, workstations).
- The default `control_listen` without a configuration entry is `127.0.0.1:11435` ([ADR-0040](adr/0040-authentication-fails-closed-control-port-on-loopback-by-default.md));
  set it (`0.0.0.0:11435` in `config.minimal.yaml`) for remote agents and browsers.

## Secrets

The router reads `/etc/skirnir-router/secrets.env` (0600) as environment: `MQTT_PASSWORD`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY` and, for the optional memory layers, the variables their configuration names. Maintain it by hand, or let
`render-env.sh` render it from a secret store with a universal-auth API: access goes into
`/etc/skirnir-router-secrets/render-env.conf` (folder 0700 root, file 0600; template
[router/render-env.conf.example](../router/render-env.conf.example)); the daily timer restarts the router when a secret
changed. If the secret store is unreachable the last file stays. The access file is read by root, so it must not lie in
a folder the router may write: `render-env.sh` refuses a file (or `SECRET_STORE_AUTH_FILE`) under `/etc/skirnir-router`,
`/var/lib/skirnir-router` or `/var/log/skirnir-router`, or one that is not owned by root or writable by group or others,
and the router unit cannot see `/etc/skirnir-router-secrets` at all. `install.sh` and `deploy.py` move an old
`/etc/skirnir-router/render-env.conf` there (ADR-0073).

## Optional: decision engine for the auto role

The auto role `auto:latest` ([configuration.md](configuration.md#auto-role-the-decision-engine)) uses up to four engines.
Two of them need files on the router host:

- `tfidf`: `decision-tfidf.json` in `/etc/skirnir-router/` (`install.sh` puts it into `/opt/skirnir-router/`; copy it, or
  point `decision_engine.tfidf.model_path` there).
- `embed`: the embedding container from [decision-embed/](../decision-embed/) (`compose.yaml`, binds only `127.0.0.1:8082`).

`config.example.yaml` ships `decision_engine.enabled: true` with the chain `[embed, local_llm, tfidf, rules]`. A missing tfidf
file or an unreachable embed container does not stop the router: it logs a warning and the chain continues with the next
engine. Without them, set `enabled: false` or a shorter chain such as `[local_llm, rules]`. `config.minimal.yaml` has no
decision engine.

## Command reference

| Command | What it does |
|---|---|
| `router.py [config.yaml]` | Run the router (default `/etc/skirnir-router/config.yaml`; log level from `ROUTER_LOG`, default `INFO`) |
| `router.py --help` | List the commands |
| `router.py --init <dir> [--public-url URL] [--user NAME] [--client NAME] [--cert PEM] [--key PEM]` | Write a first, safe configuration (see above) |
| `router.py --check <config> [--pure] [--files]` | Validate a configuration; unknown keys are reported with a suggestion. `--files`: the files it names are readable. Exit code 0 = ok, 2 = invalid |
| `router.py --posture <config> [--pure]` | What the configuration allows that the threat model calls risky (observe mode, unlocked policy, shared or example tokens, address-only clients, control port without login or TLS, API port without TLS, plaintext backends …), each with severity and next step ([ADR-0064](adr/0064-report-the-security-posture-as-facts.md)). Exit code 0 also with findings |
| `router.py --hash` | PBKDF2 hash for `control_auth.users`; asks without echo |

`--pure` ignores an adjacent `roles.yaml` (changes made in the web UI). Run the commands with the venv's Python
(`/opt/skirnir-router/venv/bin/python`) or the system `python3`; both have the required packages after `install.sh`.

## Manual installation

Without `install.sh` (another distribution, or to see each step):

1. Copy `router/` to `/opt/skirnir-router/` (including the package `skirnir_router/`) and create the venv the unit runs:
   `python3 -m venv --system-site-packages /opt/skirnir-router/venv` and
   `/opt/skirnir-router/venv/bin/pip install -r /opt/skirnir-router/requirements-session.txt`.
2. Write the configuration ([First configuration](#first-configuration)).
3. Check: `python3 /opt/skirnir-router/router.py --check /etc/skirnir-router/config.yaml` and
   `python3 /opt/skirnir-router/router.py --posture /etc/skirnir-router/config.yaml`.
4. Copy the systemd units from `router/` to `/etc/systemd/system/`: `skirnir-router.service`, optionally
   `skirnir-router-cert.path`/`.service` and `skirnir-router-secrets.*`; `systemctl daemon-reload`,
   `systemctl enable --now skirnir-router`.
5. Secrets and, if wanted, the decision engine as above.
6. Install agents and approve them in the UI ([install-agent.md](install-agent.md)).

## Security checklist

- `client_auth.mode: enforce` and `locked: true` (`--init` writes both).
- One token per client, no example token (`--posture` reports `clients_share_token` and `client_example_token`).
- `control_tls` set and `api_tls: true`.
- Port 11435 firewalled to GPU nodes and admin machines.
- Ollama on every node bound to loopback.
- Fingerprint compared with `skirnir-agent identity` before approving a node.
- `router.py --posture` run before every rollout.
- Key and secret files readable by root without capabilities (`--check <config> --files`).
- No password on a command line: `router.py --hash` without argument asks.

## Appendix: the maintainer's LXC deploy

This is the maintainer's environment-specific deploy, not a requirement. [deploy/deploy.py](../deploy/deploy.py) copies code,
configuration and units via SFTP to an LXC host, from there via file push into the container (templates `CT_EXEC`/`CT_PUSH`,
default LXD), checks the configuration before and after the push and restarts the service only if the check passes.
Everything site-specific (host, container number, production `config.yaml`, secret store access) is read from an **ops
folder outside the repo**, see [deploy/ops_env.py](../deploy/ops_env.py). That way the repo can be public without hashes, IPs
or hostnames ending up in it. If you have no LXC host, use `install.sh`, copy by hand or adapt the two templates
`CT_EXEC`/`CT_PUSH` in `deploy.env`. `deploy.py` also places signed agent releases on the router (`--agent`) and manages the
signing key (`--create-agent-key`, `--new-agent-key`, `--use-next-agent-key`, `--replace-lost-agent-key`); without it use
`deploy/agent_keys.py` ([operations.md](operations.md#agent-updates-and-rollout)).
