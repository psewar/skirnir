# Skirnir – selbst gehosteter LLM-Router und AI-Gateway für Ollama

**Deutsch** · [English](README.md)

<img src="design/skirnir-logo.png" alt="Skirnir" width="120" align="right">

Skirnir ([psewar/skirnir](https://github.com/psewar/skirnir)) ist ein selbst gehosteter LLM-Router und AI-Gateway für Ollama
mit OpenAI-kompatibler API und Anthropic Messages API. Es verteilt Anfragen GPU-bewusst auf mehrere Knoten und bietet
optionalen Cloud-Fallback sowie optionalen persistenten Sitzungskontext und gemeinsames Agentengedächtnis.

Skirnir steht vor einer oder mehreren Ollama-Installationen auf GPU-Rechnern und verhält sich gegenüber seinen Clients wie
**ein** Ollama-Server. Clients fragen nicht nach einem konkreten Modell, sondern nach einer **Rolle** (`standard:latest`,
`gross:latest`, `code:latest` …; die Rollennamen sind frei wählbar); der Router wählt pro Anfrage Knoten, Modell und
Kontextgröße – nach dem, was gerade geladen ist, ob die GPU von einem Spiel belegt wird, wie viel VRAM frei ist, wie schnell
und wie fehlerfrei ein Modell auf einem Knoten zuletzt war. Die Ollama-API bleibt unverändert, dazu kommen eine
OpenAI-kompatible Schnittstelle unter `/v1` und die Anthropic-Messages-API (`/v1/messages`, z. B. für Claude Code).

Gebaut für ein Homelab: Home Assistant, Node-RED und lokale Agenten-Frameworks als Clients, Windows-Gaming-PCs als GPU-Knoten,
die nicht rund um die Uhr laufen und nicht nur der KI gehören. Der Name: Skirnir ist in der nordischen Mythologie Freyrs Bote,
der in fremde Reiche reitet, dort verhandelt und mit der Antwort zurückkehrt.

**Stand:** Router 0.23.0, Agent 0.30.0 (siehe [Releases](https://github.com/psewar/skirnir/releases) und
[CHANGELOG.md](CHANGELOG.md), das Tags auf Router- und Agent-Versionen abbildet), seit September 2026 in einem Homelab im
Dauerbetrieb, ein Entwickler. Keine Stabilitätszusagen. Die Dokumentation gibt es auf Englisch und Deutsch, die Unter-READMEs
(Agent, Decision Engine) sind deutsch. Wer es nachbaut, sollte Python, systemd und Ollama kennen.

Sichere Entwicklung: [Threat Model](docs/security/threat-model.de.md), [Risikoregister](docs/security/risk-register.json)
und [Anleitung für PR-Review/CI](design/continuous-security-review.md). Jeder PR benötigt einen aktuellen, an die
Änderungen gebundenen Review-Nachweis; optionale Modellreviews sind beratend und brauchen einen explizit konfigurierten Endpunkt.

## Was Skirnir kann

- **Rollen statt Modellnamen.** Eine Rolle ist eine Rangliste von Stufen (`Modell @ num_ctx`, optional `busy_ok`). Der Router
  nimmt die beste Stufe, die ein Knoten jetzt bedienen kann; ein schon geladenes Modell gewinnt („warm zuerst“).
- **Mehrere GPU-Knoten**, Windows oder Linux, angebunden über einen kleinen **Agenten** (Go), der einen ausgehenden Tunnel zum
  Router hält. Kein offener Port am Knoten, kein Zertifikat, keine feste IP. Wake-on-LAN für schlafende Knoten.
- **Busy-Erkennung.** Belegt ein Spiel die GPU (fremdes VRAM, Auslastung), laufen dort nur noch `busy_ok`-Stufen; große Modelle
  werden entladen und nach dem Spielen wieder vorgewärmt.
- **Scheduler** mit Score (warm, Last, VRAM, Tempo, Fehlerrate), Circuit Breaker je Knoten und **Admission Control** mit
  Prioritäten und Deadline im Router statt in Ollamas eigener Schlange.
- **Der Client beschreibt, der Router entscheidet:** ein optionaler `routing`-Block im Request nennt Pflicht-Fähigkeiten
  (`tools`, `vision`, `thinking`, `structured` …), Mindestkontext, Priorität, Sitzung, Datenklasse.
- **Auto-Rolle** `auto:latest`: eine Kette lokaler Entscheidungs-Engines (Embedding-Modell auf CPU, kleines LLM, TF-IDF, Regeln)
  wählt die Rolle aus dem Text der Anfrage. Gemessen 93 bis 98 % Treffer.
- **Cloud als Stufe.** OpenAI, Anthropic und weitere OpenAI-kompatible Anbieter (zum Beispiel Mistral) können als letzte (oder
  erste) Stufe in Rollen liegen, mit Datenklassen, Credential-Scan, Monatsbudget und Egress-Allowlist. Standardmäßig geht nichts
  in die Cloud.
- **Client-Identitäten** auf dem Inferenz-Port (Bearer, Basic, Quell-IP), Rollen- und Modellsperren, Rate Limit, Audit-Log.
- **Beobachtbar:** Prometheus-Endpunkt, Nutzung je Tag und Client, Entscheidungsprotokoll, Home-Assistant-Gerät per MQTT-Discovery,
  Web-UI mit Live-Grafiken, Katalog, Rollen-Editor, Probierfeld und Lasttest.
- **Betriebsfest:** Config-Schema mit Vorschlägen bei Tippfehlern, Idempotency-Key, Canary- und Shadow-Stufen je Rolle,
  Deploy-Manifest mit SHA-256, Ollama-Versionen und Modell-Digests je Knoten.
- **TLS auf jeder Strecke**, sobald konfiguriert (siehe [Sicherheit](#sicherheit-das-wichtigste)), ein Prozess, ein Event-Loop,
  keine Datenbank. Abhängigkeiten: `aiohttp`, `pyyaml`, `cryptography`, optional `paho-mqtt` und `uvloop`.

## Architektur

```
  Clients: Home Assistant · Node-RED · OpenAI-kompatible Werkzeuge · Claude Code · Agenten-Frameworks
      │  https :11434   Ollama-API (/api/*), OpenAI-API (/v1/*), Anthropic-API (/v1/messages), Client-Identität
      ▼
 ┌────────────────────────── Skirnir (Debian-Container, Python 3.11, aiohttp) ──────────────────────────┐
 │  Rollen & Stufen · Scheduler · Admission · Decision Engine · Cloud-Stufen · Metriken · Usage · Audit │
 │  Web-UI und /admin/*  https :11435 (Basic Auth)              MQTT-Discovery ─────▶ Home Assistant     │
 └───────────────▲──────────────────────────────────▲──────────────────────────────────────────────────┘
                 │ wss /v1/tunnel (Agent → Router,   │ https
                 │ Ed25519-Schlüssel, Multiplex)      │
   ┌─────────────┴──────────────┐         ┌──────────┴───────────┐
   │ GPU-Knoten (Windows/Linux) │  …      │ Cloud-Anbieter       │
   │ Agent (Go) ◀▶ Ollama       │         │ OpenAI · Anthropic   │
   └────────────────────────────┘         └──────────────────────┘
```

Der Agent meldet den Zustand seines Knotens selbst. Alle 2 s schickt er GPU-Auslastung, VRAM und Sensoren (Heartbeat). Seit
Agent 0.14.0 beobachtet er außerdem sein lokales Ollama und meldet installierte Modelle (`/api/tags`), geladene Modelle
(`/api/ps`) und ob Ollama überhaupt antwortet, sofort bei jeder Änderung und spätestens alle 30 s vollständig. Aus beidem
entsteht je Knoten der Zustand `offline` / `free` / `busy`. Nur bei Knoten, deren Agent den Ollama-Zustand nicht meldet (ältere
Agenten, statische Knoten ohne Agent), fragt der Router Ollama weiterhin selbst alle 5 s ab. Alle Ollama-Aufrufe des Routers
laufen durch den Tunnel des Agenten; Ollama selbst bleibt auf `localhost`. Ports und Endpunkte:
[docs/reference.de.md](docs/reference.de.md#schnittstellen).

## Schnellstart

**1. Ohne GPU ausprobieren.** Aus einem Klon, mit Python 3.11+ und den Paketen aus `requirements-dev.txt`:

```bash
python test/run_tests.py     # einmal: erzeugt das selbstsignierte test/test-cert.pem, das der Fake-Knoten braucht (braucht openssl, ~3 min)
python test/dev_env.py       # Fake-Knoten, Fake-Cloud, Fake-Agent und Router; http://127.0.0.1:21435/ öffnen
```

Diese Umgebung läuft ohne TLS und ohne Login; der Fake-Agent `big` wartet im Tab *Agenten* auf Freigabe.

**2. Router installieren** unter Debian oder Ubuntu (Python 3.11 oder neuer), aus einem Klon oder Release:

```bash
sudo bash router/install.sh
sudo python3 /opt/skirnir-router/router.py --init /etc/skirnir-router --public-url https://<name>:11435
```

`--init` fragt das Passwort der Web-UI (mindestens 12 Zeichen), erzeugt ein Client-Token und zeigt es **einmal**, und schreibt
eine sichere Konfiguration mit der Rolle `standard`. TLS-Zertifikat und -Schlüssel für `<name>` an die Pfade in
`router.control_tls` legen (Standard `/etc/skirnir-router/certs/fullchain.pem` und `privkey.pem`), dann
`sudo systemctl enable --now skirnir-router` und `https://<name>:11435/` öffnen (Benutzer `admin`). Einzelheiten,
TLS-Varianten und Firewall: [docs/install-router.de.md](docs/install-router.de.md).

**3. GPU-Knoten hinzufügen.** Ollama installieren und an Loopback binden (`OLLAMA_HOST=127.0.0.1`), dann das Modell der Rolle
laden: `ollama pull qwen3:8b`. Den Agenten aus dem [Release](https://github.com/psewar/skirnir/releases) laden:

- Windows: `skirnir-agent-<version>-windows-amd64.exe` doppelklicken, UAC bestätigen, drei Fragen beantworten (Router-Adresse
  `https://<name>:11435`, Update-Schlüssel des Betreibers oder leer, Benutzer mit Steuerrecht für den Dienst).
- Linux: `chmod +x skirnir-agent-<version>-linux-amd64 && sudo ./skirnir-agent-<version>-linux-amd64 setup --router https://<name>:11435`

Der Knoten muss dem Zertifikat des Routers vertrauen (ein öffentliches oder eine private CA in `router.ca_file`).
Einzelheiten: [docs/install-agent.de.md](docs/install-agent.de.md).

**4. Knoten freigeben.** Er erscheint in der Web-UI im Tab *Agenten* als wartend. Auf dem Knoten `skirnir-agent identity`
ausführen (als Administrator bzw. root) und nur freigeben, wenn der Fingerprint mit dem in der UI übereinstimmt.

**5. Erste Anfrage** mit dem Client-Token aus Schritt 2, OpenAI-kompatibel:

```bash
curl https://<name>:11434/v1/chat/completions -H "Authorization: Bearer <client token>" -H "Content-Type: application/json" \
  -d '{"model":"standard:latest","messages":[{"role":"user","content":"Hello"}]}'
```

oder nativ über Ollama:

```bash
curl https://<name>:11434/api/chat -H "Authorization: Bearer <client token>" \
  -d '{"model":"standard:latest","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

Die erste Anfrage lädt das Modell und dauert länger. Mit `curl -i` zeigen die Header `X-Skirnir-Node`, `-Model` und `-Tier`,
wo sie lief.

**6. Home Assistant anbinden.** Die Integration *Ollama* mit der URL `https://<name>:11434/` und dem Client-Token als API-Key
hinzufügen; Modell ist der Rollenname (`standard:latest`). Home Assistant einen eigenen Client mit eigenem Token geben (Web-UI,
Tab *Clients*). Weitere Clients: [docs/clients.de.md](docs/clients.de.md).

## Konzepte

- **Rollen und Stufen.** Ein Client fragt nach einer Rolle; die Rolle listet Stufen (Modell und Kontextgröße) nach Qualität.
  Der Router nimmt die beste Stufe, die ein Knoten jetzt bedienen kann, und bevorzugt ein schon geladenes Modell.
  [Konfiguration](docs/configuration.de.md#rollen-und-stufen).
- **Der Client beschreibt, der Router entscheidet.** Ein optionaler `routing`-Block nennt Pflicht-Fähigkeiten, Mindestkontext,
  Priorität, Sitzung und Datenklasse; der Router wählt Knoten, Modell und Stufe. [Clients](docs/clients.de.md#der-routing-block).
- **Busy-Erkennung und Spielmodus.** Ein Spiel oder andere fremde GPU-Last macht einen Knoten `busy`: Dort laufen nur
  `busy_ok`-Stufen, große Modelle werden entladen und danach wieder vorgewärmt. [Konfiguration](docs/configuration.de.md#rollen-und-stufen).
- **Cloud als Stufe.** Ein Cloud-Modell ist nur eine weitere Stufe einer Rolle, geschützt durch Datenklassen, Credential-Scan,
  Budget und Egress-Allowlist; standardmäßig aus. [Konfiguration](docs/configuration.de.md#cloud-als-stufe-in-rollen).
- **Auto-Rolle.** `auto:latest` lässt eine Kette lokaler Engines die Rolle wählen. [Konfiguration](docs/configuration.de.md#auto-rolle-die-decision-engine).
- **Gedächtnis und Sitzungen (optional).** Session-Speicher, Hindsight Memory, verwalteter Kontext und gemeinsames Gedächtnis
  sind standardmäßig aus. [Referenz](docs/reference.de.md#optionaler-persistenter-session-speicher).

## Sicherheit: das Wichtigste

- `router.client_auth.mode: enforce` und `locked: true`, damit Unbekannte 401 bekommen und der Modus nicht in der UI
  umschaltbar ist (`router.py --init` schreibt beides).
- Ein Token je Client; nie ein Beispiel-Token stehen lassen. Gespeichert wird nur der sha256 eines Tokens.
- TLS auf beiden Ports: `router.control_tls` (cert, key) für den Steuer-Port 11435 und `router.api_tls: true` für den
  API-Port 11434. `api_tls` steht standardmäßig auf `false`; ohne `control_tls` spricht der Steuer-Port Klartext-HTTP.
- Port 11435 per Firewall auf die GPU-Knoten und Admin-Rechner beschränken; 11434 ist für die Clients.
- Ollama auf den Knoten an Loopback binden (`OLLAMA_HOST=127.0.0.1`); der Router erreicht es nur durch den Tunnel des Agenten.
- Vor der Freigabe eines Knotens den Fingerprint mit `skirnir-agent identity` vergleichen, über einen anderen Weg als das Netz,
  über das er sich angemeldet hat (TM-07).
- Vor jedem Rollout `router.py --posture /etc/skirnir-router/config.yaml` laufen lassen; das Startlog wiederholt die Befunde.
- Der Router läuft als root **ohne Capabilities** ([ADR-0044](docs/adr/0044-systemd-hardening-root-kept-for-now.md); ein
  eigener Benutzer bräuchte Rechte auf Konfiguration, Log und Zertifikat): Der TLS-Schlüssel und andere Dateien müssen für
  root als Eigentümer lesbar sein. `router.py --check <config> --files` prüft das.
- Passwörter nie auf der Befehlszeile übergeben: `router.py --hash` ohne Argument fragt ohne Echo.
- Was die Vertrauensbasis verschiebt (Ports, TLS, Identitäten, Anbieter-Endpunkte, Egress, `client_auth.mode` bei `locked`),
  ist nicht über UI oder API änderbar, nur über die Konfigurationsdatei.
- Secrets liegen in `secrets.env` (0600) und erscheinen nie in Logs oder `/admin/state`; der Router hat keinen Schreibzugriff
  auf den Secret-Store. Prompts gehen nur mit deklarierter Datenklasse in die Cloud und nie mit erkennbaren Schlüsseln darin.
- Agenten melden sich über WSS mit einer Ed25519-Challenge an; MQTT läuft über 8883. Ollama-Updates lädt der Agent selbst von
  einer festen Quelle (`ollama_update.source`, Standard GitHub-Releases von ollama/ollama) und prüft sie gegen die
  `sha256sum.txt` dieses Releases; der Router nennt nur die Version.

Meldeweg für Sicherheitslücken: [SECURITY.md](SECURITY.md).

## Dokumentation

| Dokument | Inhalt |
|---|---|
| [docs/install-router.de.md](docs/install-router.de.md) | Voraussetzungen, `install.sh`, erste Konfiguration, TLS, Firewall, Secrets, Befehle, Installation von Hand, LXC-Deploy |
| [docs/install-agent.de.md](docs/install-agent.de.md) | GPU-Knoten unter Windows und Linux, Ollama, Freigabe, Update-Schlüssel |
| [docs/configuration.de.md](docs/configuration.de.md) | Client-Identitäten, Rollen und Stufen, Scheduler und Admission, Auto-Rolle, Cloud-Stufen |
| [docs/clients.de.md](docs/clients.de.md) | Home Assistant, Node-RED, OpenAI- und Anthropic-Clients, Claude Code, der `routing`-Block |
| [docs/operations.de.md](docs/operations.de.md) | Web-UI, Metriken, Home-Assistant-Entitäten, Agent- und Ollama-Updates, Schlüsselwechsel, Namen, Messwerte |
| [docs/troubleshooting.de.md](docs/troubleshooting.de.md) | Statuscodes, wartende Agenten, TLS-Fehler, Logs, Gedächtnis-Zustände |
| [docs/reference.de.md](docs/reference.de.md) | Schnittstellen, GPU-Schutz, optionale Gedächtnis- und Sitzungsfunktionen, Rerank, OpenAI-kompatible Knoten, Grenzen, Repo-Layout |
| [docs/adr/](docs/adr/README.md) | Architecture Decision Records (englisch): warum Skirnir so gebaut ist |
| [agent/README.md](agent/README.md) | Interna des Agenten |

## Entwicklung

```bash
python test/run_tests.py          # rund 250 End-to-End-Prüfungen, ~3 min: zwei Fake-Ollama-Knoten, Fake-Agent durch den Tunnel,
                                  # Fake-Cloud (OpenAI und Anthropic), Fake-Decision-Dienst, selbstsigniertes TLS, Client-Auth,
                                  # Admission, Breaker, Canary/Shadow, Idempotency, Cloud-Schranken, UI-Config-Runden
python test/selftest_context_check.py   # Selbsttests ohne Router: Kontext-Vorabprüfung und
python test/selftest_toolcall_rescue.py   # Tool-Call-Rettung (je ein paar Sekunden)
python test/dev_env.py            # dieselbe Umgebung zum Klicken, ohne TLS/Login
python test/perf_run.py           # Messstand: CPU je Anfrage, py-spy-Profil (test/perf_profile_report.py)
ruff check .                      # Lint (ruff.toml: F, E9, B904, B905)
```

Abhängigkeiten für Betrieb und Entwicklung: `router/requirements.txt` (Laufzeit, entspricht den Debian-12-Paketen) und
`requirements-dev.txt` (Lint, Deploy, Tests). Die CI prüft bei jedem Push Lint, `go vet`/`go test`, einen Windows-Cross-Build
und die Selbsttests; die End-to-End-Suite läuft nächtlich, weil sie zeitempfindlich ist. Arbeitsablauf, Pflichten eines PR und
Projektregeln: [AGENTS.md](AGENTS.md); Release-Historie: [CHANGELOG.md](CHANGELOG.md); Repo-Layout:
[docs/reference.de.md](docs/reference.de.md#repo-layout).

## Lizenz

[MIT](LICENSE). Die Modelle, die Skirnir verteilt, und die eingebundenen Fremdprojekte (Jevlike, multilingual-e5-small,
Ollama) haben ihre eigenen Lizenzen.
