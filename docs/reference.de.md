# Referenz

**Deutsch** · [English](reference.md) · [README](../README.de.md)

## Schnittstellen

| Port | Was | Wer |
|---|---|---|
| **11434** | Ollama-API: `GET /api/tags`, `/api/ps`, `/api/version`, `POST /api/show`; Inferenz `POST /api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings` (Streaming wird durchgereicht, `model` in der Antwort trägt den Rollennamen). `/api/pull`, `push`, `create`, `copy`, `delete` → 403. TLS mit dem eigenen Zertifikat, wenn `api_tls: true` (Standard `false`). | Clients |
| **11434 `/v1`** | OpenAI-kompatibel: `GET /v1/models`, `POST /v1/chat/completions` (SSE-Streaming, `tools`, `response_format`), `/v1/completions`, `/v1/embeddings`. Der Router übersetzt selbst nach `/api/chat`, damit Rollen, Kontextstufen und `keep_alive` gelten – Ollamas eigenes `/v1` kennt weder `num_ctx` noch `keep_alive`. | OpenAI-Clients |
| **11434 `/v1/messages`** | Anthropic-Messages-API: `POST /v1/messages` (streamend und nicht), `POST /v1/messages/count_tokens`; `GET /v1/models` antwortet in Anthropic-Form, wenn die Anfrage `anthropic-version` trägt. Übersetzung auf `/api/chat` wie bei `/v1`, siehe [Anthropic-Clients](clients.de.md#anthropic-clients-claude-code). | Claude Code, Anthropic-SDKs |
| **11434 `/rerank`, `/info`** | Optionales TEI-kompatibles Reranking auf einem GPU-Knoten durch den Agent-Tunnel (standardmäßig aus, Client-Recht `rerank`), siehe [Rerank-Endpunkt](#optionaler-rerank-endpunkt). | Gedächtnisdienste |
| **11434 `/v1/skirnir/…`** | Optionale Sitzungs- und Gedächtnis-APIs, standardmäßig aus ([unten](#optionaler-persistenter-session-speicher)). | Agenten-Frameworks |
| **11435** | Web-UI (`/`), `GET /admin/state`, `GET/PUT /admin/config`, `POST /admin/try`, `/admin/loadtest`, `/admin/measure`, `/admin/bench`, `/admin/decide`, `GET /admin/decision`, `/admin/usage`, `/admin/ha`, `/admin/nodes` (`POST`/`DELETE /admin/nodes/{fp}/quiet`: Messruhe für einen Knoten, bis zu einer Zeit kein Entladen und kein Vorwärmen, [ADR-0061](adr/0061-quiet-a-single-node-for-a-measurement.md)), `/admin/clients`, `GET /metrics`. Basic Auth (PBKDF2-Hashes in der Konfiguration, geprüftes Paar 10 min gecacht). Jede Nicht-GET-Anfrage an `/admin/*` braucht zusätzlich den Header `X-Skirnir-UI: 1` (CSRF-Schutz, [ADR-0042](adr/0042-admin-writes-require-a-custom-header.md); die Web-UI sendet ihn). Standard-Listener `127.0.0.1:11435`; für entfernte Agenten und Browser `control_listen` setzen ([ADR-0040](adr/0040-authentication-fails-closed-control-port-on-loopback-by-default.md)). Ohne `control_auth.users` antwortet der Port mit 503, auch über Loopback; unangemeldeter Loopback-Zugriff nur mit `control_auth.allow_unauthenticated_loopback: true` für die Entwicklung ([ADR-0053](adr/0053-control-port-requires-users-even-on-loopback.md)). TLS mit `control_tls`. | Browser, Skripte, Prometheus |
| **11435 `/v1/tunnel`** | WebSocket vom Agenten: Anmeldung mit Ed25519-Signatur auf eine Challenge, Heartbeat, Konfigurationspaket, alle Ollama-Aufrufe als multiplexte Streams (REQ/RESP/DATA/END/ERR/CANCEL). | Agent → Router |
| MQTT 8883 | Discovery und Zustände für Home Assistant (TLS, Passwort aus `secrets.env`). | Router → Broker |

Anfragen dürfen 600 s dauern (`request_timeout_s`). Bricht der Client ab, schließt der Router die Upstream-Antwort, der Tunnel
schickt CANCEL, der Agent beendet die Ollama-Anfrage. Der Inferenz-Port nutzt TCP-Keepalive (erste Probe nach 60 s Stille, dann
alle 15 s, nach 4 ohne Antwort tot): Eine Anfrage ohne Stream kann minutenlang ohne ein Byte laufen, und irgendwo auf dem Weg zum
Client verfällt der Verbindungszustand sonst still. Die Proben halten ihn frisch und lassen eine tote Verbindung als Fehler auffallen.

## GPU-Schutz

Agent 0.7.0, [design/gpu-guard.md](../design/gpu-guard.md). Der Agent setzt ein Power-Limit von 80 % des Standardlimits (460 W
bei einer 575-W-Karte), geht nach 10 Minuten Dauer-Vollast für 5 Minuten auf 70 %, setzt nach Treiber-Resets nach, hebt nie über
den Standard und warnt bei Spannungsabfall am 16-Pin-Stecker, Speichertemperatur und Hardware-Drosselung. Standard an; die
Abwahl (`gpu_guard.enabled: false`) ist eine Entscheidung und wird deshalb als Problem gemeldet. Das Limit zu setzen braucht
unter Windows das Konto LocalSystem. Der Router deckelt einen gedrosselten Knoten auf eine Anfrage, zieht Score ab und reicht
Probleme an Home Assistant (`modes.gpu_guard`, Policy je Knoten im Agenten-Register).

## Optionaler persistenter Session-Speicher

Die API `/v1/skirnir/sessions` kann explizit übermittelte Client-Ereignisse in PostgreSQL speichern. Sie ist
**standardmäßig deaktiviert**; bestehende Installationen benötigen weder Datenbank noch zusätzliche Abhängigkeiten. Aktivierung
über `router.session_store`, Installation von `router/requirements-session.txt` (`install.sh` erledigt das), DSN aus der
konfigurierten Umgebungsvariable und Freigabe von `session_projects` je authentifiziertem Client.

Unterstützt werden Session-Metadaten/Status, unveränderliche Nachrichten, idempotente Event-IDs, paginierter Verlauf,
versionierte Task-Checkpoints sowie explizite Session-Löschung. Die authentifizierte Client-Identität bestimmt den Owner; eine
Session-ID erteilt keine Berechtigung. Token-Zugangsdaten sind auch im observe-Modus erforderlich, IP-only-Zugriff ist
ausgeschlossen. `routing.session_id` bleibt ausschließlich Knoten-/Modell-Affinität. Inferenz und Streaming bleiben gleich.

`client-managed` speichert weiterhin nur explizite Ereignisse. Hermes verwaltet Compaction und Memory selbst.
[API, Konfiguration, Migration, Rollback und Tests (Englisch)](../design/session-store.md).

## Optionales Hindsight Memory

Ein standardmäßig deaktivierter Hindsight-REST-Adapter ergänzt gezielten Recall und bewusste asynchrone Retention, unabhängig
vom Session Store. Bestehende Clients bleiben unverändert. Service-URL, Token und stabiler Namespace-Schlüssel kommen aus der
Umgebung; Client-/Projekt-Policies vergeben getrennte Lese-/Schreibrechte.

Explizites `routing.memory` nutzt den bestehenden nativen/OpenAI-/Anthropic-Chatpfad. Erinnerungen werden begrenzt, mit
Quellen versehen und als nicht vertrauenswürdige User-Daten eingefügt. Cloud-Nutzung ist pro Memory-Projekt standardmäßig
gesperrt. Retention ist separat aus; unvollständige Streams, Tool-Workflows und erkannte sensible Inhalte werden
ausgeschlossen. Hermes mit eigenem Memory-Provider behält `memory.owner: client`, damit keine doppelte Verarbeitung erfolgt.
[Konfiguration, API und Grenzen (Englisch)](../design/hindsight-memory.md). Gedächtnis-Zustände in Antworten:
[troubleshooting.de.md](troubleshooting.de.md#gedächtnis-zustände).

CI prüft neue Zeilen auf gängige identifizierende Angaben und Secrets; die [Repository-Vorgaben](../AGENTS.md) verlangen
zusätzlich eine manuelle Datenschutzprüfung. Tests und Beispiele nutzen synthetische Daten. Echte Verbindungswerte und
Memory-Daten gehören nicht ins Repository.

## Optionaler verwalteter Session-Kontext

`router.context_manager` wird zusammen mit dem Session Store aktiviert. Explizit als `skirnir-managed` angelegte Sessions
speichern neue Nachrichten aus dem bestehenden Ollama-/OpenAI-/Anthropic-Chatpfad. Clients senden nur neue Nachrichten.
Modellabhängige Budgets und unveränderliche, extraktive Compaction-Versionen begrenzen den aktiven Kontext. Systemvorgaben,
Tool-Aufrufe und Ergebnisse bleiben exakt; Ziele und offene Arbeit werden in separaten Task-Checkpoints versioniert. Der
Rohverlauf bleibt über paginierte History und geschützte Suche verfügbar. Snapshots enthalten aktiven Kontext, Version und
Herkunft; laufende Prozesse und Tools werden nicht wiederhergestellt.

Standard bleibt aus; `client-managed` und zustandslose Weiterleitung behalten ihr Verhalten. Verwaltete Inferenz unterstützt
zunächst lokale Text-/Tool-Modelle. Passt geschützter Kontext nicht ins Zielfenster, wird die Anfrage abgewiesen. Erfolg wird
erst nach dauerhafter Speicherung der vollständigen Antwort gemeldet.
[Einrichtung, Delta-/Retry-Vertrag, Grenzen und Migration/Rollback (Englisch)](../design/managed-context.md).

## Optionales gemeinsames Gedächtnis und Agent-Übergaben

Serverseitig freigegebene Memory Scopes, eine persistente PostgreSQL-Outbox, signierte Agent-Übergaben und Session-Suche
erweitern das optionale Context Gateway. Neue Strategien, Tool-Reduktion, automatische Checkpoints und Aufbewahrungsregeln
bleiben standardmäßig aus. Private Bank-IDs und bestehende Inferenz-Clients bleiben kompatibel. Siehe
[Aktivierung, Hermes-Anbindung, API und Rollback](../design/shared-memory-interoperability.de.md).

Betriebliche Verbesserungen: [Memory-Zuverlässigkeit, Recovery und echte Smoke-Tests](../design/memory-reliability.de.md).

## Optionaler Rerank-Endpunkt

`POST /rerank` auf dem Inferenz-Port spricht das TEI-Rerank-Protokoll und reicht die Anfrage durch den Agent-Tunnel an einen
Reranker-Dienst auf einem GPU-Knoten weiter (Agent-Route `rerank`, Gesundheit im Heartbeat gemeldet). Standardmäßig aus; ein
Client braucht das Recht `rerank: true` und darf an seiner Quelladresse erkannt werden. Kein WOL, keine Warteschlange, keine
Wiederholung: Kann kein Knoten reranken, antwortet der Router sofort mit `424 rerank_unavailable` und einem Grund, damit ein
Aufrufer mit eigenem CPU-Reranker ohne Verzögerung zurückfällt. Siehe [Konfiguration, Drahtformat und Gründe (Englisch)](../design/rerank.md).
Ein Reranker für Intel-iGPU, CPU oder NPU liegt in [rerank-server/](../rerank-server/).

## Optionaler OpenAI-kompatibler Knoten

Ein GPU-Knoten kann statt Ollama einen OpenAI-kompatiblen Inferenz-Server betreiben, zum Beispiel Strata, das ein großes
Mixture-of-Experts-Modell aus einer GPU plus Arbeitsspeicher bedient. Die Agent-Einstellung `tunnel.upstream_api: openai`
(Standard `ollama`) richtet den Tunnel auf diesen Server: Der Agent meldet dessen Modelle aus `/v1/models` und sagt es in
seinen Fakten. Der Router schickt Chat- und Generate-Anfragen dann als `/v1/chat/completions` durch den Tunnel, übersetzt
wie eine Cloud-Anfrage, sodass die Antwort jedes Client-Format erreicht, und lässt weg, was nur Ollama hat: kein
`/api/show` (Fähigkeiten kommen aus dem Katalog), kein Ollama-Update. Den Server als Agent-Kind (`children:`) betreiben
und das Ollama-Kind abschalten, denn beide brauchen die GPU. Sein Grafikspeicher zählt als eigener des Agenten, die
Busy-Erkennung behandelt ihn also wie ein geladenes Modell. Das Modell kommt mit `weights_gib` und seinen `capabilities` in
den Katalog und wie jedes lokale Modell in eine Rollen-Stufe. Embeddings gehen an `/v1/embeddings` (ein Server ohne diesen
Endpunkt wird dafür übersprungen), und verwaltete Session-Züge laufen wie auf einem Ollama-Knoten. Router 0.10.0, Agent 0.21.0.

**GPU freigeben fürs Spielen** (Router 0.11.0, Agent 0.22.0): Ein solcher Server kann sein Modell nicht entladen. Nennt
`tunnel.upstream_child` das Kind, das ihn betreibt, hält der Router den Server an, sobald der Knoten busy wird, und startet
ihn wieder, wenn der Knoten frei ist (dort, wo Ollama-Knoten vorgeladen werden) oder eine Anfrage das Modell braucht; diese
Anfrage wartet, bis das Modell geladen ist, höchstens drei Minuten. Der Agent meldet den Server als *angehalten* oder
*startend* statt als nicht erreichbar, und sein Home-Assistant-Gerät nimmt geladene Modelle und Grafikspeicher aus dieser
Meldung. Ohne `upstream_child` behält ein busy Knoten sein Modell und wird nur übersprungen.

**Ersatz-Ollama während eines Spiels** (Router 0.12.0, Agent 0.23.0): Solange der Server angehalten ist, hat der Knoten
nichts anzubieten, nicht einmal ein kleines `busy_ok`-Modell wie das des Sprachassistenten. Mit `tunnel.upstream_fallback`
startet der Agent an seiner Stelle ein Ollama-Kind, sobald der Server gestoppt ist, und schickt die Anfragen des Tunnels
dorthin:

```yaml
tunnel:
  upstream: http://127.0.0.1:8080
  upstream_api: openai
  upstream_child: strata
  upstream_fallback:
    child: ollama                      # ein eingeschaltetes Ollama-Kind; der Agent hält es bis zum Anhalten gestoppt
    upstream: http://127.0.0.1:11434   # Standard
    max_ram_percent: 90                # Standard; 0 = keine RAM-Prüfung vor dem Start des Servers
```

Der Agent meldet die Modelle von Ollama (OLLAMA `serving: "ollama"`) zusammen mit denen des angehaltenen Servers, und der
Router behandelt den Knoten so lange als Ollama-Knoten: `busy_ok`-Modelle laufen während des Spiels, alles andere wird wie
üblich entladen. Eine Anfrage für das Modell des Servers oder das Vorladen, sobald der Knoten frei ist, gibt den Server
wieder frei; der Agent lässt laufende Antworten auf dem Ersatz fertig laufen (höchstens zwei Minuten), stoppt ihn und
startet erst dann den Server, sodass sich beide nie die Karte teilen. Das Ollama-Update des Routers lässt den Ersatz aus
(er ist nicht das vom Agenten verwaltete Ollama).

**Angehalten starten, RAM-Prüfung vor dem Start** (Agent 0.26.0, Router 0.17.0): Der Agent merkt sich die Modelle des
Servers und seinen RAM-Bedarf (höchster privater Speicher seines Prozessbaums im letzten Lauf) in
`<identity-Ordner>/upstream.json`. Mit dieser Datei startet er mit angehaltenem Server und laufendem Ersatz – ein Neustart
lädt den Server also nicht, nur damit der Router ihn wieder anhält; der Router gibt ihn frei, wenn er ihn will (Anfrage
für sein Modell, Vorladen, Fenster). Ohne die Datei (erster Start, neuer Server) startet der Server einmal wie bisher, und
der Agent lernt beides. Vor jedem Start prüft der Agent, dass belegter RAM plus dieser Bedarf `max_ram_percent` nicht
übersteigt; sonst bleibt der Server angehalten, und der OLLAMA-Frame trägt `upstream_blocked` (Grund `memory`, verfügbar,
gesamt, Bedarf, Grenze), bis wieder Platz ist. Der Router bietet dann keinen Freigabe-Kandidaten an und antwortet mit
`memory_pressure` samt diesen Zahlen; Vorladen und Fenster senden so lange keine Freigabe.

**Für eine Anfrage auf den Ersatz umschalten** (Router 0.15.0, Agent 0.25.0): Der Agent meldet die Modelle des Ersatzes auch,
solange dieser gestoppt ist (Heartbeat `standby_models`, gelesen aus seinem Modellspeicher `OLLAMA_MODELS/manifests`). Eine
Anfrage, deren Modell nur der Ersatz hat, darf dann den Server anhalten, als konkretes Modell oder als Rollen-Stufe: Der Knoten
ist ein kalter Kandidat mit dem Abzug `modes.score.switch` (Standard 60), sodass jeder Knoten gewinnt, der das Modell ohne
Umschalten hat. Die Anfrage wartet, bis der Ersatz bedient, höchstens drei Minuten, und läuft dann als gewöhnliche
Ollama-Anfrage. Strata kommt auf dem bekannten Weg zurück: mit der nächsten Anfrage für sein Modell oder über die
Residenz-Prüfung, sobald das Modell des Ersatzes `residency_idle_s` lang ungenutzt war. Die UI markiert solche Modelle in
ihren Modelllisten mit ↻ und Modelle, die kein Knoten bedienen kann, mit ✕.
Das Anhalten stoppt den Server sofort, darum wartet eine umschaltende Anfrage seit Router 0.16.4 zuerst, bis auf dem Server
keine Antwort mehr läuft (die Tool-Aufrufe eines Agenten lassen solche Lücken), höchstens so lange wie das Wartebudget ihrer
Priorität (`modes.admission`). Kommt keine Lücke, bekommt sie 503 mit dem Grund-Code `server_busy` und `Retry-After` 30 s.

**Nur im Fenster oder auf Anfrage** (Router 0.16.0, `modes.upstream_window`, standardmäßig aus): Ein solcher Server belegt
den größten Teil der Karte. Mit eingeschaltetem Fenster hält der Router ihn für die eingestellten Stunden, zum Beispiel 01:00
bis 07:00 für unbeaufsichtigte Aufgaben in der Nacht. Tagsüber bedient das Ersatz-Ollama und wird wie jeder Ollama-Knoten
vorgewärmt.

- Außerhalb des Fensters startet eine Anfrage für das Modell des Servers, etwa über eine eigene Rolle, ihn trotzdem.
- Außerhalb des Fensters wird ein Server, der `idle_hold_s` (Standard 900 s) lang ungenutzt war, wieder angehalten. Vorwärmen,
  Residenz und das Ende eines Spiels holen ihn nicht mehr zurück.
- Innerhalb des Fensters wird ein angehaltener Server freigegeben, sobald der Knoten frei ist und der Ersatz ebenso lange
  ungenutzt war, damit eine bewusste Anfrage an den Ersatz in der Nacht nicht hin und her schaltet.

## Grenzen und Nicht-Ziele

- Ein Router-Prozess, kein Cluster: Geteilter Zustand (Warteschlange, Breaker, Register) liegt im Speicher und in JSON-Dateien.
- Kein Inhaltsklassifikator für Datenklassen (sie sind Deklarationen; die optionale Inhaltsprüfung schützt nur Cloud-Stufen),
  kein Modell-Signing über Ollamas Digests hinaus, kein OpenTelemetry.
- Die Busy-Erkennung kennt unter Windows kein prozessgenaues VRAM (WDDM liefert `N/A`); sie rechnet mit Summen und gelernten
  Baselines und hat dafür Nachlauf- und Anspruchs-Mechanik gegen Phantomwerte.
- `/v1/embeddings` braucht ein Embedding-Modell auf dem Knoten; Chat-Modelle antworten dort mit 501, das reicht der Router durch.
- Die Deploy-Skripte (`deploy/deploy.py`) setzen LXC voraus; `router/install.sh` deckt Debian und Ubuntu ab.

## Performance

Gemessen ist der Router I/O-gebunden: rund 0,6 ms CPU je weitergeleiteter Anfrage im Messstand, etwa 10 ms je `/api/chat` in
Produktion gegenüber 200–300 ms im Modell. Ein Prozess mit einem Event-Loop reicht, weil die GPU-Knoten 2–4 Anfragen/s liefern
und die eigentliche Grenze `OLLAMA_NUM_PARALLEL` ist.

## Repo-Layout

| Pfad | Inhalt |
|---|---|
| `router/router.py`, `router/skirnir_router/` | Dienst: `app`, `config` (Schema), `proxy` (Ollama-API), `openai_api`, `request` (routing-Block), `scheduler`, `admission`, `nodes`, `poll` (Zustandsautomat, Prewarm), `registry` (Agenten-Register), `tunnel`, `auth`, `cloud`, `decision/` (Engines), `metrics`, `perf`, `ops` (Idempotency, Canary, Shadow, Manifest), `ha` (MQTT), `admin`, `settings` (UI-Allowlist), `toolcall_rescue` (Tool-Calls, die Ollamas Parser als Text verliert, werden zurückgeholt), `wol`, `posture`, `setup_init` (`--init`) |
| `router/ui.html`, `router/skirnir.png`, `router/favicon.png` | Web-UI und Logo |
| `router/install.sh` | Router unter Debian/Ubuntu installieren oder aktualisieren |
| `router/config.minimal.yaml`, `router/config.example.yaml` | Minimale erste Konfiguration (Vorlage für `--init`) und volle Referenz |
| `router/*.service`, `*.timer`, `*.path`, `render-env.sh`, `render-env.conf.example` | systemd-Einheiten, Secrets-Renderer |
| `router/requirements.txt`, `router/requirements-session.txt` | Laufzeit-Abhängigkeiten (die zweite ergänzt den optionalen Session-Speicher) |
| `router/decision-tfidf.json` | trainiertes TF-IDF-Modell der Auto-Rolle |
| `agent/` | Agent für die GPU-Knoten (Go 1.27, Windows-Dienst / Linux-Binary), `Install-Service.ps1`, `Set-UpdateKey.cmd`, `sign.ps1`, eigenes README |
| `decision-eval/` | Datensatz-Generator (136 Vorlagen, 1282 Beispiele, Gruppen-Split), Trainer für TF-IDF und Jevlike, Auswertung (Top-1, Kalibrierung, Ketten), Ergebnisse |
| `decision-embed/` | Stufe 2 der Auto-Rolle: ONNX-Export, Kopf-Training, Dienst, Container |
| `decision-jevlike/` | Jevlike-Dienst (Prototyp, gemessen, nicht produktiv) |
| `rerank-server/` | Optionaler Cross-Encoder-Reranker mit TEI-Protokoll `/rerank` auf OpenVINO (Intel-iGPU, CPU, NPU), als Kind des Agenten für einen Gedächtnisdienst wie Hindsight; eigenes README |
| `deploy/` | `agent_keys.py` (Signierschlüssel und signierte Agent-Releases, eigenständig), `deploy.py` und `ops_env.py` (LXC-Deploy des Betreuers) |
| `design/` | Entwürfe: `routing-algorithm.md`, `roadmap.md` (Ausbaustufen und Entscheidungen), `decision-engine.md`, `gpu-guard.md`, Entwürfe zu Gedächtnis und Sitzungen; Logos (mit einem Bildmodell erzeugt, Metadaten entfernt) |
| `docs/` | Benutzerdokumentation (dieser Ordner, Englisch und Deutsch), `docs/adr/` (Architecture Decision Records, MADR 4.0, englisch, Verzeichnis in `docs/adr/README.md`), `docs/security/` (Threat Model, Risikoregister, Reviews) |
| `test/` | Testsuite, Fakes, Dev-Umgebung, Messstand |
| `tools/` | Messskripte für den GPU-Schutz (Lasttest, Nachtlauf Stufe 2) |
| `monitoring/` | Prometheus + Grafana neben dem Router (Compose, Scrape-Konfiguration, provisioniertes Dashboard, Secret-Renderer, `deploy_ct.py`), eigenes README |
| `.github/workflows/ci.yml`, `CHANGELOG.md`, `SECURITY.md`, `AGENTS.md` | CI (Lint, Go vet/test, Windows-Cross-Build, Selbsttests; die End-to-End-Suite nächtlich), Release-Historie, Meldeweg für Sicherheitslücken, Entwicklungsablauf |
