# Einen GPU-Knoten einrichten

**Deutsch** · [English](install-agent.md) · [README](../README.de.md)

Ein GPU-Knoten betreibt Ollama und den Skirnir-Agenten. Der Agent hält einen ausgehenden Tunnel zum Router: kein offener Port
am Knoten, kein Zertifikat, kein Token und keine feste IP. Interna des Agenten: [agent/README.md](../agent/README.md).

## Was ein Knoten braucht

| Was | Einzelheiten |
|---|---|
| GPU | NVIDIA mit funktionierendem Treiber (`nvidia-smi`) |
| Ollama | Installiert, an Loopback gebunden (`OLLAMA_HOST=127.0.0.1`), mit den Modellen der Rollen geladen |
| Agent | Release-Datei `skirnir-agent-<version>-windows-amd64.exe` oder `skirnir-agent-<version>-linux-amd64` aus den [Releases](https://github.com/psewar/skirnir/releases) |
| Netz | Ausgehend HTTPS zu `https://<name>:11435`; dem Zertifikat des Routers muss der Knoten vertrauen (öffentlich oder eine private CA in `router.ca_file`) |
| Update-Schlüssel (optional) | Der öffentliche Ed25519-Schlüssel des Betreibers für Selbst-Updates ([unten](#updates-und-der-update-schlüssel)) |
| Wake-on-LAN (optional) | Im BIOS und am Netzwerkadapter eingeschaltet („Wake on Magic Packet“); die MAC kennt der Router vom Agenten, WOL erlaubst du bei der Freigabe |

## Ollama vorbereiten

1. Ollama installieren und die Modelle der Rollen laden, z. B. `ollama pull qwen3:8b` für die Rolle in `config.minimal.yaml`.
   Der Router routet nur Rollen, deren Modelle ein Knoten hat.
2. Ollama an Loopback binden: `OLLAMA_HOST=127.0.0.1`. Der Agent erreicht Ollama über `127.0.0.1:11434`; lauscht Ollama auf
   allen Adressen, ist es am Router vorbei erreichbar (TM-11). Unter Linux mit dem systemd-Dienst von Ollama:
   `sudo systemctl edit ollama`, `[Service]` und `Environment="OLLAMA_HOST=127.0.0.1"` eintragen, dann
   `sudo systemctl restart ollama`. Unter Windows als Umgebungsvariable setzen oder Ollama als Kind des Agenten betreiben
   ([unten](#ollama-als-kind-des-agenten)).
3. `OLLAMA_NUM_PARALLEL` sollte `max_inflight` des Knotens im Router entsprechen (Standard 2,
   [configuration.de.md](configuration.de.md#scheduler-circuit-breaker-admission)).
4. Windows: Der Ollama-Installer legt Firewall-Regeln `ollama.exe` an, die jede Quelle zulassen. Diese entfernen, Ollama braucht
   keinen eingehenden Zugriff: `Get-NetFirewallRule -DisplayName 'ollama.exe' | Remove-NetFirewallRule` in einer
   Administrator-PowerShell, oder `Install-Service.ps1 -RemoveOpenOllamaRules` (siehe [Windows-Sonderfälle](#windows-sonderfälle)).

## Windows: Einrichtung per Doppelklick

Seit Agent 0.10.0: `skirnir-agent-<version>-windows-amd64.exe` laden, doppelklicken (oder mit `setup` aufrufen), UAC
bestätigen und drei Fragen beantworten:

1. Router-Adresse, z. B. `https://<name>:11435`,
2. öffentlicher Update-Schlüssel des Betreibers (base64; leer = Selbst-Update später ergänzen),
3. der Benutzer, der den Dienst ohne Administratorrechte steuern darf.

Danach schreibt setup `C:\ProgramData\skirnir-agent\config.yaml`, kopiert sich nach `C:\Program Files\skirnir-agent\`, richtet
den Dienst *Skirnir Agent* ein (technischer Name `SkirnirAgent`, LocalSystem, Autostart, Neustart nach Absturz), legt die
GPU-Z-Relay-Aufgabe an (bei Anmeldung, ohne Adminrechte), startet den Dienst und zeigt Gesundheit und Fingerprint. Der Schlüssel
entsteht beim ersten Start unter `…\identity\` (DPAPI-geschützt), Logs liegen unter `…\logs\`. Der Knoten erscheint in der
Web-UI im Tab *Agenten* zur Freigabe.

Ein erneuter Aufruf von setup aktualisiert an Ort und Stelle. Bei vorhandener Konfiguration bietet es an, einen fehlenden
Update-Schlüssel zu ergänzen und ein altes `NT SERVICE`-Konto auf LocalSystem umzustellen (nötig für den GPU-Schutz);
Identität und Logs bleiben. Das Windows-setup fragt nicht nach einer CA: Für eine private CA `ca_file` unter `router:` in der
Konfiguration ergänzen ([unten](#dem-router-zertifikat-vertrauen)).

### Windows-Sonderfälle

`agent/Install-Service.ps1` bleibt nur für Sonderfälle: alte geplante Aufgaben entfernen, die den Agenten betrieben, und die
offenen Firewall-Regeln des Ollama-Installers. Es erwartet eine Konfiguration unter `C:\ProgramData\skirnir-agent\config.yaml`
und eine gebaute `agent\dist\skirnir-agent.exe` daneben; als Administrator ausführen, z. B.
`powershell -ExecutionPolicy Bypass -File agent\Install-Service.ps1 -RemoveOpenOllamaRules`. Weitere Schalter:
`-RemoveOllamaLanRules`, `-SkipGpuzRelay`, `-LegacyTasks <namen>`.

## Linux: setup

Seit Agent 0.30.0 ([ADR-0070](adr/0070-linux-agent-setup-and-private-router-ca.md)):

```bash
chmod +x skirnir-agent-<version>-linux-amd64
sudo ./skirnir-agent-<version>-linux-amd64 setup --router https://<name>:11435 [--update-key <öffentlicher Schlüssel>] [--ca-file <pem>]
```

Ohne `--router` fragt setup Router-Adresse und Update-Schlüssel im Terminal ab. Bevor es handelt, prüft es root, eine
`https://`-Router-Adresse, das Format des Update-Schlüssels (base64, 32 Byte) und die CA-Datei. Dann

- schreibt es `/etc/skirnir-agent/config.yaml` (nur für den Eigentümer; nur wenn keine existiert, eine vorhandene bleibt
  unverändert) mit Router-URL, `router.ca_file`, `tunnel.upstream: http://127.0.0.1:11434`, `update.public_key`,
  `identity.dir` und `logging.dir`,
- legt es `/var/lib/skirnir-agent` (der Knotenschlüssel, Modus 0600, liegt in dessen Ordner `identity`) und
  `/var/log/skirnir-agent` an,
- installiert es die Binary als `/usr/local/bin/skirnir-agent` und die Unit `/etc/systemd/system/skirnir-agent.service`,
- prüft es die Konfiguration wie `check-config`, führt `systemctl daemon-reload` und `systemctl enable --now skirnir-agent` aus,
- zeigt es den `identity`-Befehl für den Fingerprint-Vergleich. Die letzte Zeile ist `STATE: installed`.

**Stattdessen von Hand:**

```bash
sudo install -m 755 skirnir-agent-<version>-linux-amd64 /usr/local/bin/skirnir-agent
sudo mkdir -p /etc/skirnir-agent /var/lib/skirnir-agent /var/log/skirnir-agent
sudo cp config.yaml /etc/skirnir-agent/config.yaml    # aus agent/config.example.yaml; identity.dir und logging.dir unter /var setzen
sudo cp agent/skirnir-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now skirnir-agent
```

Die Unit startet `skirnir-agent run --config /etc/skirnir-agent/config.yaml` mit `ProtectSystem=full`; schreiben darf sie nur
`/var/lib/skirnir-agent`, `/var/log/skirnir-agent` und `/usr/local/bin` (das Selbst-Update legt die neue Binary neben die alte).
Prüfen mit `skirnir-agent status --config /etc/skirnir-agent/config.yaml` und `journalctl -u skirnir-agent -f`.

## Ollama als Kind des Agenten

Der Agent kann Ollama als Kind-Prozess führen (`children:` in seiner Konfiguration), damit ein Knoten nach dem Reboot ohne
Anmeldung bereit ist; weitere Dienste beaufsichtigt er auf dieselbe Weise. Unter Windows ersetzt das die Tray-App:

- Benutzer-Umgebungsvariablen gelten für das Dienstkonto nicht, darum kommen `OLLAMA_HOST=127.0.0.1:11434`, `OLLAMA_MODELS`,
  `OLLAMA_NUM_PARALLEL` usw. ins `env` des Kindes.
- Die Tray-App `ollama app.exe` darf daneben **nicht** laufen (sie versucht ständig, einen eigenen Server zu starten); ihren
  Autostart abschalten.
- Ollama-Updates kommen dann über den Router ([operations.de.md](operations.de.md#ollama-updates)); der Updater der Tray-App
  greift nicht mehr.

Der Beispieleintrag `children:` für Ollama samt den nötigen Ordnerrechten steht in
[agent/config.example.yaml](../agent/config.example.yaml). Nach einer Änderung der Konfiguration `skirnir-agent check-config`
ausführen und den Dienst neu starten (`skirnir-agent restart` unter Windows, `systemctl restart skirnir-agent` unter Linux).

## Dem Router-Zertifikat vertrauen

Der Agent prüft das Zertifikat des Routers für den Tunnel und für Update-Downloads mit dem Zertifikatsspeicher des
Betriebssystems. Für eine private CA:

```yaml
router:
  url: https://<name>:11435
  ca_file: /etc/skirnir-agent/router-ca.pem   # zusätzlich zum Systemspeicher vertraut
```

(`setup --ca-file` unter Linux schreibt das). `check-config` lehnt eine unlesbare Datei oder eine ohne PEM-Zertifikat ab.

## Knoten freigeben

Ein neuer Agent erzeugt beim ersten Start seinen Ed25519-Schlüssel, meldet sich an und wartet in der Web-UI im Tab *Agenten*
als *wartend* (Punkt am Tab; Home-Assistant-Sensor *Knoten wartet auf Freigabe*). Vor der Freigabe:

1. Auf dem Knoten als Administrator bzw. root `skirnir-agent identity` ausführen (Windows:
   `"C:\Program Files\skirnir-agent\skirnir-agent.exe" identity`; Linux: `sudo skirnir-agent identity`).
2. Den Fingerprint mit dem in der UI vergleichen, über einen anderen Weg als das Netz, über das sich der Knoten angemeldet hat
   (TM-07).
3. Freigeben und Wake-on-LAN, Gewicht und das MQTT-Gerät des Knotens einstellen.

Nach der Freigabe bekommt der Agent sein Konfigurationspaket durch den Tunnel (Heartbeat-Takt, optional MQTT-Zugang) und
braucht keine eigenen Secrets. Kein Token, keine Firewall-Regel, keine IP von Hand.

## Was der Agent tut

- Baut den Tunnel zum Router (`wss://…:11435/v1/tunnel`) und hält ihn mit Backoff; ein Router-Neustart kostet 1–2 s.
- Weist sich mit dem beim ersten Start erzeugten **Ed25519-Schlüssel** aus (Windows: DPAPI-geschützt); unbekannte Schlüssel
  warten im Router auf Freigabe.
- Meldet alle 2 s GPU-Auslastung und VRAM (NVML direkt, Rückfall `nvidia-smi`), Hostname, MAC, Versionen; seit Agent 0.6.0 auch
  Temperatur, Leistung und Power-Limit, Lüfter, Drosselgründe und, wenn auf einem Windows-Knoten GPU-Z läuft,
  Speichertemperatur, Hot Spot, GPU-Spannung sowie Leistung und Spannung am 16-Pin-Stecker (alles im Knotenzustand des Routers
  und als Home-Assistant-Sensoren). Außerdem meldet er den Zustand von Ollama, laufende Spiele und freien Hauptspeicher
  ([configuration.de.md](configuration.de.md#rollen-und-stufen)).
- Bekommt nach der Freigabe sein Konfigurationspaket durch den Tunnel.
- Kann Ollama und weitere Dienste als Kinder führen (oben).
- Stellt optional einen TLS-Proxy vor Ollama (`ollama_proxy`, Port 11443, Zertifikat per Fingerprint gepinnt) für Router, die
  ohne Tunnel direkt zugreifen sollen.
- **Schützt die GPU** mit einem Power-Limit, standardmäßig an ([reference.de.md](reference.de.md#gpu-schutz)).

## Updates und der Update-Schlüssel

Agenten aktualisieren sich über den Router: Der Betreiber legt Binaries mit einem Manifest auf den Router, signiert mit einem
**Betreiber-Schlüssel** (nie vom Router); die Agenten holen sie über ihre ausgehende Verbindung, prüfen Signatur und Hash,
tauschen sich selbst und starten neu. Ohne `update.public_key` lehnt ein Agent jeden Update-Auftrag ab.

Den Schlüssel einmal erzeugen, auf einem anderen Rechner als dem Router, nur mit Python und `cryptography`
([ADR-0068](adr/0068-standalone-agent-key-and-release-tool.md)):

```bash
python deploy/agent_keys.py --keys <schlüsselordner> create      # erster Schlüssel; zeigt den öffentlichen Teil für update.public_key
python deploy/agent_keys.py --keys <schlüsselordner> state       # gibt es einen Schlüssel? (öffentlicher Teil)
```

Den öffentlichen Teil an setup geben (Windows Frage 2, Linux `--update-key`). Bei einem Knoten, der ohne ihn eingerichtet
wurde, setup erneut ausführen (Windows fragt danach) oder unter Windows `agent\Set-UpdateKey.cmd -Step Add -Key "<öffentlicher Schlüssel>"`
in einer Administrator-Eingabeaufforderung (mit Sicherung, `check-config` und Rückweg; `-Step Check` zeigt die vertrauten
Schlüssel). Ein Release veröffentlichen:

```bash
python deploy/agent_keys.py --keys <schlüsselordner> sign-release <dist> --version <v> [--out <ordner>]
```

schreibt `manifest.json` und die nach dem Release benannten Binaries (Standard `<dist>/publish`); diese nach
`/etc/skirnir-router/agent/` auf den Router kopieren. Rollout, Schlüsselwechsel (`next`, `use-next`) und ein verlorener
Schlüssel (`replace-lost "<grund>"`): [operations.de.md](operations.de.md#agent-updates-und-rollout).

## Agent-Befehle

| Befehl | Was er tut |
|---|---|
| `setup` | Installieren oder aktualisieren (Windows: Standard ohne Verb, Doppelklick; Linux: `sudo skirnir-agent setup [--router URL] [--update-key KEY] [--ca-file PEM]`) |
| `install`, `uninstall`, `apply-rules` | Dienst anlegen oder entfernen, ACLs, Firewall-Regeln und Steuerrecht neu setzen (Windows, Administrator) |
| `start`, `stop`, `restart` | Dienst steuern (Windows; ohne Administratorrechte, sobald `install` das Steuerrecht vergeben hat) |
| `status` | Dienstzustand und `/health` des laufenden Agenten |
| `run` | Im Vordergrund laufen (Konsole/systemd, Strg-C beendet) |
| `identity` | Fingerprint und öffentlicher Schlüssel, zum Vergleich mit der Router-UI vor der Freigabe |
| `check-config` | Konfiguration laden und prüfen |
| `gpu` | Messquelle (NVML/nvidia-smi) und alle Sensoren einmal, GPU-Z eingeschlossen |
| `game` | Spielerkennung und Hauptspeicher einmal (Spielordner, Programmnamen, Treffer) |
| `mqtt-clear` | Die retained Discovery-Konfigurationen dieses Geräts löschen (Testgeräte aufräumen) |
| `gpuz-relay` | GPU-Z-Sensoren aus der Anmeldesitzung an den Dienst reichen (Windows; Aufgabe bei Anmeldung) |
| `version` | Version |

Jedes Verb nimmt `--config <pfad>` (Standard `C:\ProgramData\skirnir-agent\config.yaml` bzw. `/etc/skirnir-agent/config.yaml`).

## Konfigurationsprüfung

Der Agent lehnt eine Konfiguration mit einem Schlüssel auf oberster Ebene ab, den er nicht liest (ein vertippter oder in einer
früheren Version entfernter Block), und nennt den Schlüssel. `skirnir-agent check-config` zeigt das vor einem Neustart; ein
Selbst-Update endet dann als fehlgeschlagener Auftrag, und die alte Binary läuft weiter (Agent 0.28.0,
[ADR-0051](adr/0051-agent-rejects-unknown-top-level-configuration-keys.md)).
