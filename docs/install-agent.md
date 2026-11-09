# Installing a GPU node

**English** · [Deutsch](install-agent.de.md) · [README](../README.md)

A GPU node runs Ollama and the Skirnir agent. The agent keeps an outbound tunnel to the router: no open port on the node,
no certificate, no token and no fixed IP. Agent internals (German): [agent/README.md](../agent/README.md).

## What a node needs

| What | Details |
|---|---|
| GPU | NVIDIA with a working driver (`nvidia-smi`) |
| Ollama | Installed, bound to loopback (`OLLAMA_HOST=127.0.0.1`), with the models of your roles pulled |
| Agent | Release asset `skirnir-agent-<version>-windows-amd64.exe` or `skirnir-agent-<version>-linux-amd64` from the [releases](https://github.com/psewar/skirnir/releases) |
| Network | Outbound HTTPS to `https://<name>:11435`; the router's certificate must be trusted (public, or a private CA in `router.ca_file`) |
| Update key (optional) | The operator's public Ed25519 key for self-updates ([below](#updates-and-the-update-key)) |
| Wake-on-LAN (optional) | Enabled in BIOS and network adapter ("Wake on Magic Packet"); the router learns the MAC from the agent, you allow WOL when approving |

## Prepare Ollama

1. Install Ollama and pull the models your roles use, e.g. `ollama pull qwen3:8b` for the role in `config.minimal.yaml`.
   The router routes only roles whose models a node has.
2. Bind Ollama to loopback: `OLLAMA_HOST=127.0.0.1`. The agent reaches Ollama on `127.0.0.1:11434`; listening on all
   addresses makes Ollama reachable around the router (TM-11). On Linux with the Ollama systemd service:
   `sudo systemctl edit ollama`, add `[Service]` and `Environment="OLLAMA_HOST=127.0.0.1"`, then `sudo systemctl restart ollama`.
   On Windows set it as an environment variable, or run Ollama as a child of the agent ([below](#ollama-as-a-child-of-the-agent)).
   On a Windows node that already runs, `agent\Set-OllamaLoopback.cmd` does it in one step (double-click; it asks for
   administrator rights): it detects whether Ollama is the agent's child or a user's desktop app, sets
   `OLLAMA_HOST=127.0.0.1:11434` in the right place, restarts Ollama, checks that it listens on loopback only (else it
   restores the previous state) and disables firewall rules that only opened Ollama. It refuses while other machines are
   connected to Ollama. `-Check` only reports; the log lands next to the script (ADR-0072).
3. `OLLAMA_NUM_PARALLEL` should match the node's `max_inflight` in the router (default 2,
   [configuration.md](configuration.md#scheduler-circuit-breaker-admission)).
4. Windows: the Ollama installer creates firewall rules `ollama.exe` that allow any source. Remove them, Ollama needs no
   inbound access: `Get-NetFirewallRule -DisplayName 'ollama.exe' | Remove-NetFirewallRule` in an administrator PowerShell,
   or `Install-Service.ps1 -RemoveOpenOllamaRules` (see [Windows special cases](#windows-special-cases)).

## Windows: setup by double-click

Since agent 0.10.0: download `skirnir-agent-<version>-windows-amd64.exe`, double-click it (or run it with `setup`), confirm
the UAC prompt and answer three questions:

1. router address, e.g. `https://<name>:11435`,
2. the operator's public update key (base64; empty = add self-update later),
3. the user allowed to control the service without administrator rights.

Setup then writes `C:\ProgramData\skirnir-agent\config.yaml`, copies itself to `C:\Program Files\skirnir-agent\`, installs the
service *Skirnir Agent* (technical name `SkirnirAgent`, LocalSystem, automatic start, restart after a crash), creates the
GPU-Z relay task (at logon, without admin rights), starts the service and shows its health and fingerprint. The key is
created on first start under `…\identity\` (DPAPI-protected), logs go to `…\logs\`. The node appears in the web UI, tab
*Agents*, for approval.

Running setup again updates in place. With an existing configuration it offers to add a missing update key and to switch an
old `NT SERVICE` account to LocalSystem (needed for the GPU guard); identity and logs stay. The Windows setup asks no CA
question: for a private CA add `ca_file` under `router:` in the configuration ([below](#trusting-the-routers-certificate)).

### Windows special cases

`agent/Install-Service.ps1` remains for special cases only: removing old scheduled tasks that ran the agent, and the Ollama
installer's open firewall rules. It expects a configuration at `C:\ProgramData\skirnir-agent\config.yaml` and a built
`agent\dist\skirnir-agent.exe` next to it; run it as administrator, e.g.
`powershell -ExecutionPolicy Bypass -File agent\Install-Service.ps1 -RemoveOpenOllamaRules`. Further switches:
`-RemoveOllamaLanRules`, `-SkipGpuzRelay`, `-LegacyTasks <names>`.

## Linux: setup

Since agent 0.30.0 ([ADR-0070](adr/0070-linux-agent-setup-and-private-router-ca.md)):

```bash
chmod +x skirnir-agent-<version>-linux-amd64
sudo ./skirnir-agent-<version>-linux-amd64 setup --router https://<name>:11435 [--update-key <public key>] [--ca-file <pem>]
```

Without `--router` setup asks for the router address and the update key on the terminal. Before acting it checks root, an
`https://` router address, the update key format (base64, 32 bytes) and the CA file. Then it

- writes `/etc/skirnir-agent/config.yaml` (owner-only; only if none exists, an existing one stays unchanged) with the router
  URL, `router.ca_file`, `tunnel.upstream: http://127.0.0.1:11434`, `update.public_key`, `identity.dir` and `logging.dir`,
- creates `/var/lib/skirnir-agent` (the node key, mode 0600, lives in its `identity` folder) and `/var/log/skirnir-agent`,
- installs the binary as `/usr/local/bin/skirnir-agent` and the unit `/etc/systemd/system/skirnir-agent.service`,
- validates the configuration like `check-config`, runs `systemctl daemon-reload` and `systemctl enable --now skirnir-agent`,
- prints the `identity` command for the fingerprint comparison. The last line is `STATE: installed`.

**By hand instead:**

```bash
sudo install -m 755 skirnir-agent-<version>-linux-amd64 /usr/local/bin/skirnir-agent
sudo mkdir -p /etc/skirnir-agent /var/lib/skirnir-agent /var/log/skirnir-agent
sudo cp config.yaml /etc/skirnir-agent/config.yaml    # from agent/config.example.yaml; set identity.dir and logging.dir under /var
sudo cp agent/skirnir-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now skirnir-agent
```

The unit runs `skirnir-agent run --config /etc/skirnir-agent/config.yaml` with `ProtectSystem=full`; it may write only
`/var/lib/skirnir-agent`, `/var/log/skirnir-agent` and `/usr/local/bin` (the self-update places the new binary next to the
old one). Check with `skirnir-agent status --config /etc/skirnir-agent/config.yaml` and `journalctl -u skirnir-agent -f`.

## Ollama as a child of the agent

The agent can run Ollama as a child process (`children:` in its configuration) so that a node is ready after a reboot
without anyone logging in; it can supervise further services the same way. On Windows this replaces the tray app:

- User environment variables do not apply to the service account, so `OLLAMA_HOST=127.0.0.1:11434`, `OLLAMA_MODELS`,
  `OLLAMA_NUM_PARALLEL` and friends go into the child's `env`.
- The tray app `ollama app.exe` must **not** run next to it (it keeps trying to start its own server); disable its autostart.
- Ollama updates then come through the router ([operations.md](operations.md#ollama-updates)); the tray app's own updater no
  longer applies.

The example `children:` entry for Ollama, including the folder rights it needs, is in
[agent/config.example.yaml](../agent/config.example.yaml). After editing the configuration run `skirnir-agent check-config`
and restart the service (`skirnir-agent restart` on Windows, `systemctl restart skirnir-agent` on Linux).

## Trusting the router's certificate

The agent verifies the router's certificate for the tunnel and for update downloads with the operating system's trust
store. For a private CA, set

```yaml
router:
  url: https://<name>:11435
  ca_file: /etc/skirnir-agent/router-ca.pem   # trusted in addition to the system store
```

(`setup --ca-file` on Linux writes it). `check-config` refuses an unreadable file or one without a PEM certificate.

## Approving the node

A new agent creates its Ed25519 key on first start, logs in and waits as *pending* in the web UI, tab *Agents* (a dot on the
tab; the Home Assistant sensor *Node awaiting approval*). Before approving:

1. On the node, as administrator or root, run `skirnir-agent identity` (Windows:
   `"C:\Program Files\skirnir-agent\skirnir-agent.exe" identity`; Linux: `sudo skirnir-agent identity`).
2. Compare the fingerprint with the one in the UI, over another channel than the network the node enrolled through (TM-07).
3. Approve, and set Wake-on-LAN, weight and the node's MQTT device.

After approval the agent receives its configuration package through the tunnel (heartbeat interval, optional MQTT access) and
needs no secrets of its own. No token, no firewall rule, no IP by hand.

## What the agent does

- Builds the tunnel to the router (`wss://…:11435/v1/tunnel`) and keeps it up with backoff; a router restart costs 1–2 s.
- Identifies itself with the **Ed25519 key** generated on first start (Windows: DPAPI-protected); unknown keys wait in the
  router for approval.
- Reports GPU utilisation and VRAM every 2 s (NVML directly, fallback `nvidia-smi`), hostname, MAC, versions; since agent 0.6.0
  also temperature, power draw and limit, fan, throttle reasons and, if GPU-Z is running on a Windows node, memory
  temperature, hot spot, GPU voltage and 16-pin connector power and voltage (all forwarded to the router state and as Home
  Assistant sensors). It also reports Ollama's state, running games and free main memory
  ([configuration.md](configuration.md#roles-and-tiers)).
- Receives its configuration package through the tunnel after approval.
- Can run Ollama and further services as children (above).
- Forwards to Ollama only what the router needs: `POST /api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings`,
  `/api/show`, `GET /api/tags`, `/api/ps`, `/api/version` and, for OpenAI-compatible servers, `POST /v1/chat/completions`,
  `/v1/embeddings`, `GET /v1/models`. Anything else - model management such as `/api/pull` or `/api/delete`, blob uploads -
  is answered 403 `path_not_allowed_on_node` without reaching Ollama, which runs with the agent's service rights
  (agent 0.31.0, ADR-0073). Models are pulled on the node itself.
- Optionally provides a TLS proxy in front of Ollama (`ollama_proxy`, port 11443, certificate pinned by fingerprint) for
  routers that should access it directly without the tunnel.
- **Guards the GPU** with a power limit, on by default ([reference.md](reference.md#gpu-guard)).

## Updates and the update key

Agents update themselves through the router: the operator places binaries with a manifest signed by an **operator key** (never
by the router) on the router; agents pull them through their outbound connection, verify signature and hash, swap themselves
and restart. Without `update.public_key` an agent refuses every update order.

Create the key once, on a machine other than the router, with only Python and `cryptography`
([ADR-0068](adr/0068-standalone-agent-key-and-release-tool.md)):

```bash
python deploy/agent_keys.py --keys <key folder> create      # first key; prints the public part for update.public_key
python deploy/agent_keys.py --keys <key folder> state       # is there a key? (public part)
```

Give the public part to `setup` (Windows question 2, Linux `--update-key`). For a node installed without it, run setup again
(Windows asks for it) or, on Windows, `agent\Set-UpdateKey.cmd -Step Add -Key "<public key>"` in an administrator prompt
(backup, `check-config` and rollback included; `-Step Check` shows the trusted keys). To publish a release:

```bash
python deploy/agent_keys.py --keys <key folder> sign-release <dist> --version <v> [--out <dir>]
```

writes `manifest.json` and the release-named binaries (default `<dist>/publish`); copy them to `/etc/skirnir-router/agent/`
on the router. Rollout, rotation (`next`, `use-next`) and a lost key (`replace-lost "<reason>"`):
[operations.md](operations.md#agent-updates-and-rollout).

## Agent commands

| Command | What it does |
|---|---|
| `setup` | Install or update (Windows: the default without a verb, double-click; Linux: `sudo skirnir-agent setup [--router URL] [--update-key KEY] [--ca-file PEM]`) |
| `install`, `uninstall`, `apply-rules` | Create or remove the service, set ACLs, firewall rules and control right again (Windows, administrator) |
| `start`, `stop`, `restart` | Control the service (Windows; without administrator rights once `install` granted control) |
| `status` | Service state and `/health` of the running agent |
| `run` | Run in the foreground (console/systemd, Ctrl-C ends it) |
| `identity` | Fingerprint and public key, to compare with the router UI before approving |
| `check-config` | Load and check the configuration |
| `gpu` | Measurement source (NVML/nvidia-smi) and all sensors once, GPU-Z included |
| `game` | Game detection and main memory once (game folders, program names, matches) |
| `mqtt-clear` | Delete this device's retained discovery configs (clean up test devices) |
| `gpuz-relay` | Pass GPU-Z sensors from the login session to the service (Windows; task at logon) |
| `version` | Version |

Every verb takes `--config <path>` (default `C:\ProgramData\skirnir-agent\config.yaml` or `/etc/skirnir-agent/config.yaml`).

## Configuration check

The agent refuses a configuration with a top-level key it does not read (a misspelt block, or one retired in an earlier
version) and names the key. `skirnir-agent check-config` shows this before a restart; a self-update then ends as a failed order
and the old binary keeps running (agent 0.28.0, [ADR-0051](adr/0051-agent-rejects-unknown-top-level-configuration-keys.md)).
