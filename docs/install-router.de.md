# Installation des Routers

**Deutsch** · [English](install-router.md) · [README](../README.de.md)

Der Router ist ein Python-Prozess mit einer systemd-Unit. Diese Seite beschreibt eine Neuinstallation unter Debian oder
Ubuntu, das TLS-Zertifikat, die Firewall, Secrets, die optionale Decision Engine und die Kommandozeilenwerkzeuge. Docker,
Podman, Unraid, TrueNAS oder Synology: siehe [Container](#container). GPU-Knoten stehen in
[install-agent.de.md](install-agent.de.md), die Konfiguration in [configuration.de.md](configuration.de.md).

## Voraussetzungen

| Was | Einzelheiten |
|---|---|
| Betriebssystem | Debian 12 oder Ubuntu (oder vergleichbar; `install.sh` unterstützt Debian und Ubuntu, sonst [Installation von Hand](#installation-von-hand)). Ein Systemcontainer (LXC) genügt; für Docker oder Podman das Router-Image ([Container](#container)). Unter Windows läuft der Router mit dem Standard-Event-Loop (Entwicklung). |
| Python | 3.11 oder neuer |
| Pakete | `python3`, `python3-venv`, `python3-aiohttp`, `python3-yaml`, `python3-cryptography`, `python3-paho-mqtt`, `python3-uvloop`, `curl`, `jq` (`install.sh` installiert fehlende). `python3-paho-mqtt` braucht es nur für Home Assistant über MQTT (`install.sh --without-mqtt` lässt es weg). Was die Distribution nicht oder zu alt mitbringt (unter Debian 12: `asyncpg` für einen PostgreSQL-Session-Store), kommt nur aus der hash-gesperrten `router/requirements-lock.txt` ins venv (ADR-0074). Ein SQLite-Session-Store (`sqlite:///...`, ADR-0114) nutzt das `sqlite3` von Python und braucht SQLite 3.37 oder neuer (Debian 12 hat 3.40). |
| Ports | **11434** für Clients (Ollama-, OpenAI- und Anthropic-API), **11435** für den Tunnel der Agenten, die Web-UI, `/admin/*` und `/metrics` |
| Name und Zertifikat | Ein DNS-Name, den Clients und Knoten verwenden (`<name>`, z. B. `router.example.net`), und ein TLS-Zertifikat dafür, siehe [TLS-Zertifikat](#tls-zertifikat) |

## Installation mit install.sh

Aus dem Ordner `router/` eines Klons oder Releases ([ADR-0069](adr/0069-router-install-script.md)):

```bash
sudo bash router/install.sh            # installieren oder aktualisieren
sudo bash router/install.sh --check    # nur melden, was fehlt oder falsch ist, nichts ändern
```

Bevor es etwas ändert, prüft das Skript: root, Debian/Ubuntu, Python 3.11 oder neuer, ein vollständiger Quellordner. Dann

1. installiert es fehlende Pakete mit `apt`,
2. ersetzt es die Programmdateien in `/opt/skirnir-router` (`router.py`, das Paket `skirnir_router/`, `ui.html` und `ui.js`, die
   Beispielkonfigurationen, `decision-tfidf.json`, `render-env.sh` …),
3. legt es das venv `/opt/skirnir-router/venv` an (`--system-site-packages`); `skirnir_router/pydeps.py` nennt die
   Anforderungen aus `requirements-session.txt`, die die Distribution nicht erfüllt, und pip installiert genau diese aus
   `requirements-lock.txt` mit `--require-hashes --no-deps` - feste Versionen, jedes Artefakt gegen seinen SHA-256
   geprüft; eine unerfüllte Anforderung ohne Eintrag in der Sperrdatei bricht die Installation ab (ADR-0074),
4. kopiert es die systemd-Units nach `/etc/systemd/system/` und führt `systemctl daemon-reload` aus,
5. fasst es die Konfiguration nie an: Ohne `/etc/skirnir-router/config.yaml` nennt es den `--init`-Befehl als nächsten Schritt;
   mit einer prüft es sie so, wie der Dienst sie liest (`router.py --check --files` ohne Capabilities, danach `--posture`), und
   sagt, ob der Dienst zu starten oder neu zu starten ist.

Die letzte Zeile ist immer `STATE: installed`, `STATE: checked` oder `STATE: refused` (mit dem Grund darüber).
**Aktualisieren** = das Skript aus dem neuen Release erneut ausführen, danach `systemctl restart skirnir-router`. Optionen:
`--prefix DIR` (Standard `/opt/skirnir-router`), `--config-dir DIR` (Standard `/etc/skirnir-router`); `--without-mqtt` für
einen Router ohne Home Assistant: Die MQTT-Bibliothek (`python3-paho-mqtt`, `paho-mqtt` aus der Sperrdatei) wird nicht
installiert, und das Skript lehnt ab, solange `config.yaml` einen MQTT-Broker nennt (`mqtt.host`); ohne die Option wird sie
wie bisher installiert (ADR-0104). `--root DIR`, `--no-venv` und `--os-release FILE` sind für Paketbau und Tests (`--help`
listet sie).

## Erste Konfiguration

Am einfachsten ([ADR-0067](adr/0067-guided-first-configuration.md)):

```bash
sudo python3 /opt/skirnir-router/router.py --init /etc/skirnir-router --public-url https://<name>:11435
```

`--init` schreibt `/etc/skirnir-router/config.yaml` aus [router/config.minimal.yaml](../router/config.minimal.yaml):

- fragt das Passwort für Web-UI und Admin-API ohne Echo (auf einem Terminal zweimal, mindestens 12 Zeichen) und speichert nur
  seinen PBKDF2-Hash unter `control_auth.users` (Benutzer `admin` oder `--user <name>`),
- erzeugt ein Client-Token (`--client <name>`, Standard `my-client`), zeigt es **einmal** und speichert nur seinen sha256,
- setzt `client_auth.mode: enforce`, `locked: true`, `api_tls: true` und die Rolle `standard` mit `qwen3:8b`,
- übernimmt die Zertifikatspfade aus `--cert` und `--key` (Standard `/etc/skirnir-router/certs/fullchain.pem` und `privkey.pem`),
- legt die Datei nur für den Eigentümer lesbar an (0600) und verweigert, wenn sie schon existiert; danach laufen dieselben
  Prüfungen wie bei `--check` und `--posture`. Die letzte Zeile ist `INIT: written` oder `INIT: refused`.

Stattdessen von Hand: [router/config.example.yaml](../router/config.example.yaml) (die volle Referenz) oder
`config.minimal.yaml` nach `/etc/skirnir-router/config.yaml` kopieren (Modus 0600) und `public_url`, Zertifikatspfade, Rollen,
Katalog und Clients anpassen. Passwort-Hashes für die UI erzeugt `python3 router.py --hash` (fragt ohne Echo); der Hash eines
Clients ist der sha256 seines Tokens:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'    # ein neues Token
printf '%s' '<token>' | sha256sum                                # sein token_sha256
```

Was die einzelnen Abschnitte bedeuten: [configuration.de.md](configuration.de.md).

## TLS-Zertifikat

Der Steuer-Port nutzt `router.control_tls` (`cert`, `key`); mit `router.api_tls: true` nutzt der API-Port dasselbe
Zertifikat. `api_tls` steht standardmäßig auf `false`, und ein Steuer-Port ohne `control_tls` spricht Klartext-HTTP; also beides
setzen (`config.minimal.yaml` und `--init` tun es).

Zwei Wege:

- **Ein öffentliches Zertifikat**, z. B. von Let's Encrypt. Ist der Router aus dem Internet nicht erreichbar, die
  DNS-01-Challenge nutzen (ein ACME-Client mit Plugin für den DNS-Anbieter). Agenten, Home Assistant und andere Clients
  vertrauen ihm ohne weitere Schritte.
- **Eine private CA.** Dann braucht jeder Agent `router.ca_file: <pem>` (unter Linux `setup --ca-file`, siehe
  [install-agent.de.md](install-agent.de.md#dem-router-zertifikat-vertrauen)), und jeder Client muss der CA ebenfalls vertrauen
  (Home Assistant, Skripte, Browser).

Der Dienst läuft als root **ohne Capabilities** ([ADR-0044](adr/0044-systemd-hardening-root-kept-for-now.md)): root liest dann
nur Dateien, deren Eigentümerrechte es erlauben, wie jeder andere Benutzer. Den Schlüssel root gehören lassen, Modus 0600 (oder
0640), und die Ordner auf dem Pfad durchlässig halten. Ein Schlüssel, der nur über eine Gruppe oder einen anderen Benutzer
lesbar ist, scheitert beim Start. So prüfen, wie der Dienst liest:

```bash
sudo setpriv --inh-caps=-all --bounding-set=-all /opt/skirnir-router/venv/bin/python \
  /opt/skirnir-router/router.py --check /etc/skirnir-router/config.yaml --files
```

`skirnir-router-cert.path` startet den Router neu, wenn sich die Zertifikatsdatei ändert. Die Unit beobachtet
`/etc/skirnir-router/certs/fullchain.pem`, den Pfad, den `router.py --init` und beide Beispielkonfigurationen nutzen;
liegt `control_tls.cert` anderswo, `PathChanged=` in `/etc/systemd/system/skirnir-router-cert.path` anpassen. Einschalten mit
`systemctl daemon-reload && systemctl enable --now skirnir-router-cert.path`.

Der Router liest Zertifikat und Schlüssel nur. Seine Unit zeigt ihm `/etc/skirnir-router/certs` und `/etc/skirnir-router/tls`
(den Ordner von Installationen, die vor `router.py --init` eingerichtet wurden) nur lesend, ein kompromittierter Router kann
sie also nicht ersetzen ([ADR-0073](adr/0073-close-router-paths-to-host-privileges.md),
[ADR-0082](adr/0082-older-certificate-folder-read-only-for-the-router.md)). Der Rest von `/etc/skirnir-router` bleibt für den
Router beschreibbar: Zertifikat und Schlüssel in einen dieser beiden Ordner legen oder außerhalb der Ordner, die der Router
schreiben darf (`/etc/skirnir-router`, `/var/lib/skirnir-router`, `/var/log/skirnir-router`).
`certs` und `tls` als echte Ordner anlegen, nicht als symbolische Links: systemd schützt das Ziel eines Links, den Link
selbst könnte der Router aber ersetzen.

## Dienst starten

```bash
sudo systemctl enable --now skirnir-router
journalctl -u skirnir-router -f
```

Das Startlog listet die Befunde der Posture-Prüfung (Zeilen `posture …`). Danach `https://<name>:11435/` öffnen und anmelden.

| Unit | Zweck |
|---|---|
| `skirnir-router.service` | Der Router. Startet `/opt/skirnir-router/venv/bin/python router.py /etc/skirnir-router/config.yaml` unter eigenem Konto `skirnir-router` ohne Capabilities, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=tmpfs`, `PrivateTmp`, `UMask=0077` (ADR-0086). Schreibt nur seinen Zustandsordner `/var/lib/skirnir-router` (`router.state_dir`) und seinen Log-Ordner `/var/log/skirnir-router`; `/etc/skirnir-router` bleibt root und wird über die Gruppe gelesen. Liest `secrets.env` als Umgebung. `install.sh` und `deploy.py` legen das Konto an und setzen Besitzer und Modi mit `router-layout.sh`. |
| `skirnir-router-cert.path` + `.service` | Optional: Neustart bei erneuertem Zertifikat (siehe oben). |
| `skirnir-router-secrets.service` | Rendert `secrets.env`, bevor der Router startet (`render-env.sh`, aus einem Secret-Store oder einer Datei nur für root); ohne `render-env.conf` tut es nichts. |
| `skirnir-router-secrets.timer` + `-secrets-refresh.service` | Optional: rendert täglich und startet den Router bei geändertem Secret neu. |

## Firewall

- **11435**: nur von den GPU-Knoten (Tunnel der Agenten) und Admin-Rechnern (Web-UI, Prometheus). Die Agenten verbinden sich
  ausgehend dorthin; die Knoten brauchen keine eingehende Regel.
- **11434**: von den Clients (Home Assistant, Node-RED, Arbeitsplätze).
- Ohne Eintrag in der Konfiguration ist `control_listen` `127.0.0.1:11435` ([ADR-0040](adr/0040-authentication-fails-closed-control-port-on-loopback-by-default.md));
  für entfernte Agenten und Browser setzen (`0.0.0.0:11435` in `config.minimal.yaml`).

## Secrets

Der Router liest `/etc/skirnir-router/secrets.env` (root, 0640 für seine Gruppe `skirnir-router`) als Umgebung: `MQTT_PASSWORD`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, die in `EXTRA_SECRET_KEYS` genannten Schlüssel (weitere Cloud-Anbieter, ADR-0080) und für die optionalen
Gedächtnisschichten die Variablen, die deren Konfiguration nennt. Jede davon ist optional: `MQTT_PASSWORD` zählt nur mit
einem `mqtt:`-Abschnitt. Es gibt drei Wege, sie bereitzustellen:

1. **Von Hand**: `secrets.env` selbst schreiben (`KEY=value` je Zeile, root:skirnir-router 0640) und keine
   `render-env.conf` anlegen; `render-env.sh` rendert dann nichts (`STATE: not-configured`).
2. **`render-env.sh` aus einer Datei nur für root** (`SECRET_SOURCE=file`, `SECRET_FILE=...` in `render-env.conf`): Der
   Renderer kopiert die Datei mit denselben Prüfungen wie bei einem Store - nur eingebaute und in `EXTRA_SECRET_KEYS`
   genannte Namen, kein Wert mit Zeilenumbruch, ein doppelt genannter Name wird übersprungen. Die Datei muss root gehören,
   nur für root lesbar sein (0600) und ausserhalb der Pfade des Routers liegen, z. B.
   `/etc/skirnir-router-secrets/secrets.source.env`. Sie wird Zeile für Zeile gelesen, nie per `.` eingelesen.
3. **`render-env.sh` aus einem Secret-Store** mit Universal-Auth-API (`SECRET_SOURCE=store`, der Standard): Die
   Zugangsdaten kommen in `render-env.conf` (`SECRET_STORE_DOMAIN`, `SECRET_STORE_CLIENT_ID`, `SECRET_STORE_CLIENT_SECRET`
   oder `SECRET_STORE_AUTH_FILE`, `PROJECT_ID`, `ENVIRONMENT`).

`render-env.conf` liegt in `/etc/skirnir-router-secrets/` (Ordner 0700 root, Datei 0600; Vorlage
[router/render-env.conf.example](../router/render-env.conf.example)). Der Renderer arbeitet in drei Schritten (ADR-0104):
Er prüft, ob seine Quelle bereit ist, und sagt es (Store: nicht erreichbar, Anmeldung abgelehnt oder bereit; Datei: fehlt,
abgelehnt oder bereit), holt dann jeden Namen einzeln (vorhanden, fehlt oder Abruf fehlgeschlagen) und schreibt danach jeden
vorhandenen Namen. Die bestehende Datei bleibt, wenn die Quelle nicht bereit ist, wenn ein Abruf fehlschlug (ein
vorübergehender Fehler lässt kein Secret wegfallen) und wenn die Quelle keinen der Namen enthält. Die letzte Zeile ist
`STATE: written`, `unchanged`, `kept`, `not-configured` oder `refused` ([operations.de.md](operations.de.md#secrets-renderer)).
Der tägliche Timer startet den Router neu, wenn sich die Datei geändert hat.

systemd `LoadCredential=` ist eine Alternative für die Werte, die der Router aus einer Datei liest: `mqtt.password_file` oder
das `secrets_file` eines Cloud-Anbieters auf das Credential zeigen lassen (`/run/credentials/skirnir-router.service/<name>`,
eine `KEY=value`-Datei). Die Variablen von Session-Store und Gedächtnis kommen nur aus der Umgebung und brauchen
`secrets.env` (oder ein anderes `EnvironmentFile`). Ein Client für einen weiteren Store (z. B. Vault) ist nicht eingebaut; er
wäre eine weitere Quelle in `render-env.sh`.

`render-env.conf` und `SECRET_STORE_AUTH_FILE` liest root per `.` ein, sie dürfen also nicht in einem Ordner liegen, den der
Router schreiben darf: `render-env.sh` lehnt eine solche Datei (und `SECRET_FILE`) unter `/etc/skirnir-router`,
`/var/lib/skirnir-router` oder `/var/log/skirnir-router` ab, ebenso eine, die nicht root gehört oder für Gruppe oder andere
schreibbar ist, und die Router-Unit sieht `/etc/skirnir-router-secrets` gar nicht. `install.sh` und `deploy.py` verschieben
eine alte `/etc/skirnir-router/render-env.conf` dorthin (ADR-0073).

## Optional: Decision Engine für die Auto-Rolle

Die Auto-Rolle `auto:latest` ([configuration.de.md](configuration.de.md#auto-rolle-die-decision-engine)) nutzt bis zu vier
Engines. Zwei davon brauchen Dateien auf dem Router-Host:

- `tfidf`: `decision-tfidf.json` in `/etc/skirnir-router/` (`install.sh` legt sie nach `/opt/skirnir-router/`; kopieren oder
  `decision_engine.tfidf.model_path` dorthin zeigen lassen).
- `embed`: der Embedding-Container aus [decision-embed/](../decision-embed/) (`compose.yaml`, bindet nur `127.0.0.1:8082`).

`config.example.yaml` liefert `decision_engine.enabled: true` mit der Kette `[embed, local_llm, tfidf, rules]`. Eine fehlende
tfidf-Datei oder ein nicht erreichbarer embed-Container hält den Router nicht auf: Er loggt eine Warnung, und die Kette geht mit
der nächsten Engine weiter. Ohne beides `enabled: false` setzen oder eine kürzere Kette wie `[local_llm, rules]`.
`config.minimal.yaml` enthält keine Decision Engine.

## Befehlsübersicht

| Befehl | Was er tut |
|---|---|
| `router.py [config.yaml]` | Router starten (Standard `/etc/skirnir-router/config.yaml`; Log-Stufe aus `ROUTER_LOG`, Standard `INFO`) |
| `router.py --help` | Befehle auflisten |
| `router.py --init <dir> [--public-url URL] [--user NAME] [--client NAME] [--cert PEM] [--key PEM]` | Erste, sichere Konfiguration schreiben (siehe oben) |
| `router.py --check <config> [--pure] [--files]` | Konfiguration prüfen; unbekannte Schlüssel kommen mit Vorschlag. `--files`: die genannten Dateien sind lesbar. Exit-Code 0 = in Ordnung, 2 = ungültig |
| `router.py --posture <config> [--pure]` | Was die Konfiguration erlaubt, das das Bedrohungsmodell als riskant führt (observe-Modus, entsperrte Policy, geteilte oder Beispiel-Tokens, Clients nur per Adresse, Steuer-Port ohne Login oder TLS, API-Port ohne TLS, Klartext-Backends …), je mit Schwere und nächstem Schritt ([ADR-0064](adr/0064-report-the-security-posture-as-facts.md)). Exit-Code 0 auch mit Befunden. Das Startlog meldet zusätzlich TLS-Dateien, die der laufende Router ändern oder ersetzen könnte (`tls_files_changeable`, [ADR-0083](adr/0083-router-reports-tls-files-it-could-change.md)); der Befehl läuft außerhalb der Unit und sieht das nicht |
| `router.py --hash` | PBKDF2-Hash für `control_auth.users`; fragt ohne Echo |

`--pure` ignoriert eine danebenliegende `roles.yaml` (Änderungen aus der Web-UI). Die Befehle mit dem Python des venv
(`/opt/skirnir-router/venv/bin/python`) oder dem System-`python3` ausführen; nach `install.sh` haben beide die nötigen Pakete.

## Installation von Hand

Ohne `install.sh` (andere Distribution, oder um jeden Schritt zu sehen):

1. `router/` nach `/opt/skirnir-router/` kopieren (mit dem Paket `skirnir_router/`) und das venv anlegen, das die Unit startet:
   `python3 -m venv --system-site-packages /opt/skirnir-router/venv` und
   `/opt/skirnir-router/venv/bin/pip install -r /opt/skirnir-router/requirements-session.txt`.
2. Konfiguration schreiben ([Erste Konfiguration](#erste-konfiguration)).
3. Prüfen: `python3 /opt/skirnir-router/router.py --check /etc/skirnir-router/config.yaml` und
   `python3 /opt/skirnir-router/router.py --posture /etc/skirnir-router/config.yaml`.
4. systemd-Units aus `router/` nach `/etc/systemd/system/` kopieren: `skirnir-router.service`, optional
   `skirnir-router-cert.path`/`.service` und `skirnir-router-secrets.*`; `systemctl daemon-reload`,
   `systemctl enable --now skirnir-router`.
5. Secrets und, falls gewünscht, die Decision Engine wie oben.
6. Agenten installieren und in der UI freigeben ([install-agent.de.md](install-agent.de.md)).

## Container

Das Router-Image führt dasselbe Programm mit derselben `config.yaml` und denselben Pfaden aus wie `install.sh`
([ADR-0116](adr/0116-an-official-container-image-for-the-router.md),
[ADR-0121](adr/0121-the-router-image-installs-its-own-complete-hash-lock-on-the-official-python-image.md); wie es
funktioniert: [design/router-container.md](../design/router-container.md)). `router/Dockerfile` baut auf dem per Digest
festgelegten `python:3.15-slim-trixie` auf und installiert Python-Pakete nur aus der hash-gesperrten
`router/requirements-container-lock.txt` (nur Wheels, amd64 und arm64). Der Router läuft als uid/gid 10001
(`skirnir-router`) ohne Capabilities; im Image steckt kein Secret. Ein Image in einer Registry gibt es noch nicht: aus
einem Release oder Klon bauen.

```bash
docker build -t skirnir-router router/                  # Podman: podman build --format docker -t skirnir-router router/
```

(`--format docker` behält den Health-Check des Images, den Podmans Standard-Image-Format verwirft; `compose.yaml`
wiederholt ihn.)

| Im Container | Von | Inhalt |
|---|---|---|
| `/etc/skirnir-router` | eigener Ordner `config/`, **nur lesbar** | `config.yaml`, `certs/fullchain.pem`, `certs/privkey.pem` |
| `/var/lib/skirnir-router` | Volume `state` | `router.state_dir`: in der Web-UI geänderte Rollen, Knotenregister, Journale |
| `/var/log/skirnir-router` | Volume `logs` | `client_auth.audit_log` |
| Umgebung | `secrets.env` | die Variablen, die die `*_env`-Schlüssel nennen: `MQTT_PASSWORD`, `OPENAI_API_KEY`, `SKIRNIR_SESSION_DSN` … |
| stdout | `docker compose logs router` | das Log des Routers, mit dem Posture-Bericht beim Start |

**Erster Start.**

1. `router/compose.yaml` in einen eigenen Ordner kopieren (außerhalb des Klons: er wird die Secrets enthalten) und dort
   arbeiten.
2. Erste Konfiguration, von `--init` als root in einem Wegwerf-Container geschrieben, damit `config.yaml` root gehört
   und für die Gruppe des Routers lesbar ist, wie auf einem Host:

   ```bash
   mkdir config
   docker run --rm -it --user 0:0 -v "$PWD/config:/etc/skirnir-router" skirnir-router \
     --init /etc/skirnir-router --public-url https://<name>:11435
   ```

   Es fragt das Passwort der Web-UI, zeigt das Client-Token **einmal** und gibt den Posture-Bericht aus.
3. Zertifikat und Schlüssel für `<name>` nach `config/certs/` legen (`fullchain.pem`, `privkey.pem`) und den Ordner der
   Gruppe 10001 geben, für sie lesbar, nicht schreibbar:
   `sudo chown -R root:10001 config && sudo chmod -R g+rX,g-w,o-rwx config`. Mit rootless Podman ist die Gruppe 10001
   des Containers eine untergeordnete Gruppe des eigenen Benutzers: stattdessen `podman unshare chown -R 0:10001 config`.
4. Secrets: `secrets.env` neben `compose.yaml`, je Zeile ein `KEY=value` für die `*_env`-Schlüssel der Konfiguration;
   die Datei darf leer sein (`touch secrets.env && chmod 600 secrets.env`).
5. `docker compose up -d`, dann `docker compose ps` (Zustand `healthy`) und `docker compose logs router`.

`compose.yaml` startet den Router als uid 10001 auf einem nur lesbaren Root-Dateisystem, ohne Capabilities und ohne
Rechteerweiterung; schreibbar sind nur die beiden Volumes und ein kleines `/tmp`. Beide Ports werden auf allen
Host-Adressen veröffentlicht: per Firewall einschränken wie unter [Firewall](#firewall), oder hinter einem Reverse
Proxy `127.0.0.1:11434:11434` schreiben. Docker leitet veröffentlichte Ports weiter, bevor die üblichen Regeln von ufw
und firewalld sie sehen; in Dockers Kette `DOCKER-USER` einschränken oder auf einer Host-Adresse veröffentlichen
(`192.0.2.10:11435:11435`). Ein Bind-Mount statt eines Volumes für Zustand oder Logs muss uid 10001
gehören (`sudo chown 10001:10001 <ordner>`, Modus 0700). Unraid, TrueNAS und Synology: dieselben Mounts, dieselbe
Umgebung, Benutzer 10001:10001 und die Nur-lesen-Option in deren Container-Einstellungen.

**Befehle** laufen in einem Wegwerf-Container mit den Mounts und dem Konto des Dienstes, z. B.
`docker compose run --rm router --check /etc/skirnir-router/config.yaml --files`, `... --posture
/etc/skirnir-router/config.yaml`, `... --hash`. **Aktualisieren:** das Image des neuen Releases unter demselben Namen
bauen und `docker compose up -d` ausführen; der Zustand bleibt im Volume.

**Was die systemd-Installation erledigt und hier selbst bereitzustellen ist:** Im Image gibt es keinen
Secret-Renderer (`render-env.sh` und sein Timer): `secrets.env` selbst pflegen oder die Secrets des Orchestrators
nutzen, nach einer Änderung `docker compose up -d`. Es gibt keine Zertifikats-Unit (`skirnir-router-cert.path`): auf dem
Host nach `config/certs/` erneuern, dann `docker compose restart router`. `router-layout.sh` läuft nicht: Der nur lesbare
Mount hält den Router davon ab, Konfiguration und Zertifikate zu ändern, deshalb führt der Posture-Bericht sie nicht als
änderbar.

**Optionale Dienste** bleiben aus, solange ihr Profil nicht genannt wird: `docker compose --profile sessions up -d`
fügt PostgreSQL für `router.session_store` hinzu (Passwort in einer eigenen `postgres.env`; der Posture-Bericht behält
`session_store_unverified_tls`, bis PostgreSQL TLS hat), `--profile decision` den Embedding-Entscheidungsdienst (vorher
`docker build -t skirnir-embed decision-embed/`, Endpunkt `http://embed:8082`). Agenten bleiben native Programme auf den
GPU-Knoten und verbinden sich wie bei jedem Router mit `https://<name>:11435`.

Auf einem einzelnen Host braucht der Session-Store kein PostgreSQL: `SKIRNIR_SESSION_DSN=sqlite:///sessions.db` in `secrets.env` legt ihn als nur für den Besitzer lesbare Datei im Volume `state` ab (ADR-0114, das Image hat das `sqlite3` von Python); gesichert wird er wie `sessions.db` in [Betrieb](operations.de.md#was-sichern).

## Sicherheits-Checkliste

- `client_auth.mode: enforce` und `locked: true` (`--init` schreibt beides).
- Ein Token je Client, kein Beispiel-Token (`--posture` meldet `clients_share_token` und `client_example_token`).
- `control_tls` gesetzt und `api_tls: true`.
- Zertifikat und Schlüssel in `/etc/skirnir-router/certs`, `/etc/skirnir-router/tls` oder außerhalb der Ordner, die
  der Router schreiben darf; sonst meldet das Startlog `tls_files_changeable`.
- Port 11435 per Firewall auf GPU-Knoten und Admin-Rechner beschränkt.
- Ollama auf jedem Knoten an Loopback gebunden.
- Fingerprint vor der Freigabe eines Knotens mit `skirnir-agent identity` verglichen.
- `router.py --posture` vor jedem Rollout.
- Konfiguration und Schlüssel für das Konto des Routers lesbar (`--check <config> --files` als dieses Konto;
  `router-layout.sh` setzt Gruppe und Modus, `router-layout.sh --check` meldet).
- Kein Passwort auf der Befehlszeile: `router.py --hash` ohne Argument fragt.

## Anhang: LXC-Deploy des Betreuers

Das ist das umgebungsspezifische Deploy des Betreuers, keine Voraussetzung. [deploy/deploy.py](../deploy/deploy.py) kopiert
Code, Konfiguration und Units per SFTP auf einen LXC-Host, von dort per Datei-Push in den Container (Vorlagen
`CT_EXEC`/`CT_PUSH`, Standard LXD), prüft die Konfiguration vor und nach dem Push und startet den Dienst nur bei bestandener
Prüfung neu. Alles Standortspezifische (Host, Container-Nummer, Produktiv-`config.yaml`, Secret-Store-Zugang) liest es aus
einem **Ops-Ordner außerhalb des Repos**, siehe [deploy/ops_env.py](../deploy/ops_env.py). So kann das Repo öffentlich sein,
ohne dass Hashes, IPs oder Hostnamen darin landen. Wer keinen LXC-Host hat, nimmt `install.sh`, kopiert von Hand oder passt
die zwei Vorlagen `CT_EXEC`/`CT_PUSH` in `deploy.env` an. `deploy.py` legt auch signierte Agent-Releases auf den Router
(`--agent`) und verwaltet den Signierschlüssel (`--create-agent-key`, `--new-agent-key`, `--use-next-agent-key`,
`--replace-lost-agent-key`); ohne es dient `deploy/agent_keys.py` ([operations.de.md](operations.de.md#agent-updates-und-rollout)).
