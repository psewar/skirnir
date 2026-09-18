# Ausbau zur Inference- und Scheduling-Schicht – Fahrplan (2026-09-09)

Grundlage: Erweiterungsanforderungen des Betreibers (64 Abschnitte, 2026-09-08 abends). Dieses Dokument hält fest,
was davon schon steht, wo die Spezifikation an bewussten Entscheidungen dieses Routers reibt, und in welcher
Reihenfolge gebaut wird. Die Reihenfolge hat der Betreiber am 2026-09-09 entschieden.

## Entscheidungen

| Frage | Entscheidung | Folge |
|---|---|---|
| Cloud-Fallback? | ~~Opt-in pro Client~~ **Geändert 2026-09-10 (Betreiber): der Router legt in den Rollen fest, wo Cloud eine Stufe sein darf; der Client kann sich ausnehmen (Opt-out).** Standard der Rollen bleibt lokal. | Datenklassen sind eine *Deklaration* des Clients mit serverseitiger Untergrenze, kein Inhaltsklassifikator. Hausregel „Lokal vor Cloud" bleibt: kaltes lokales Modell vor Cloud, Default-Datenklasse personal sperrt Cloud, bis ein Client anderes deklariert. |
| Reihenfolge nach dem Fundament? | **Client-Authentifizierung zuerst.** | Der Inferenz-Port 11434 hat heute keine Authentifizierung (Basic Auth nur auf 11435). Das ist die einzige offene Sicherheitslücke und wird vor allem anderen geschlossen. |
| Paketschnitt jetzt? | **Ja, als Stufe 0.** | `router.py` (2826 Zeilen) wird in das Paket `ollama_router/` zerlegt – rein mechanisch, kein neues Verhalten, die 97 Tests sind das Netz. |

## Was schon steht

Ein Drittel der Spezifikation ist vorhanden, ein Drittel ist Erweiterung des Bestehenden, ein Drittel Neubau.

| Bereich | Heute | Lücke |
|---|---|---|
| Workloads mit Modellliste (§1) | Rollen mit geordneten Tiers | Alias `workload:x`; sonst da |
| Capabilities (§1) | aus Ollama (`vision, tools, thinking, completion`) | kein Filter auf Requests, nicht konfigurierbar |
| Features/Limits (§2) | Gewichte + KV-Kosten für den VRAM-Fit | `contextTokens`, `maxOutputTokens`, `maxImages` |
| Auswahl + Score (§3–4) | Tier → Fit → busy → `rank(warm, inflight, VRAM, Gewicht)` | Gewichte nicht konfigurierbar |
| Laufzeitdaten (§5–6) | tok/s, TTFT, Ladezeit (passiv + Bench) | EWMA, Erfolgs-/Timeout-/Structured-Output-Raten |
| Prioritäten, Queues, Admission (§7–8) | `inflight`-Zähler | fehlt |
| Cancellation (§9) | CANCEL-Rahmen im Tunnel | Weg Client-Disconnect → CANCEL prüfen |
| Session Affinity (§10) | – | fehlt (klein) |
| Residency (§11–12) | warm zuerst, prewarm, keep_alive, unload_on_busy | `minimumLoadedTime`, `unloadCooldown`, `preferredWarmModels` |
| Cloud (§13–17, 22–25, 29–30) | – | fehlt (grösster Block) |
| Datenklassen, DLP (§18–21) | – | fehlt; nur mit Cloud relevant |
| Client-Auth, Rechte, Rate Limits (§26–28) | **nichts auf 11434** | fehlt |
| Circuit Breaker (§31) | `offline_after_misses` | Zustände CLOSED/OPEN/HALF_OPEN |
| Egress, SSRF, TLS (§32–35) | TLS überall, Knoten nur aus dem Register, Ed25519-Tunnel | mTLS wäre redundant zum Schlüssel-Tunnel |
| Router führt keine Tools aus (§37) | trifft zu | – |
| Request-Limits, Providerparameter (§41–42) | `/api/pull` → 403, `keep_alive` vom Router | Client darf `num_ctx`; Grössenlimits fehlen |
| Supply Chain (§43) | – | fehlt |
| Konfiguration aktivieren (§44) | Probe-Config validiert vor dem Schreiben | kein Schema |
| Idempotency, Retry (§45–46) | – | fehlt |
| Canary, Shadow (§47–48) | – | fehlt |
| Logging, Audit (§49–51) | Entscheidungsprotokoll, kein Prompt-Logging | kein Audit-Log |
| Routing-Info, Usage (§52–54) | im State, HA-Sensoren | nicht in der Antwort |
| Prometheus, OTel (§55–56) | HA über MQTT | fehlt |
| OpenAI-kompatibel (§57) | `/v1` mit Rollen als Modellnamen | `routing`-Block |

## Wo die Spezifikation an diesem Router reibt

1. **Sie kennt das Kernproblem nicht.** Kein Wort davon, dass ein Worker gleichzeitig Spielrechner ist. Busy/free aus fremdem VRAM, Baseline-Lernen, prewarm gegen Ruckler, Sicherheitsnetz-Unload, WOL – das bleibt **erstklassiger Scheduler-Eingang**, nicht ein dünnes „verfügbare Hosts bestimmen".
2. **Tiers tragen Kontext und `busy_ok`.** Die Spezifikation trennt Modellliste und Requirements. Die Kontextleiter (`@64k → @32k`) ist aber der Mechanismus, mit dem der Router bei knappem VRAM fündig wird. Vereinbar: Requirement als Untergrenze, der Fit wählt den grössten passenden Kontext. Umbauentscheidung, keine Umbenennung.
3. **Modellnamen sind Schnittstelle.** HA, Node-RED, AI-Tasks rufen `standard:latest`. `workload:standard` kommt nur als Alias dazu.
4. **„Lokal vor Cloud" ist Hausregel.** Deshalb Cloud als Opt-in, nicht `prefer-local` als Standard.
5. **Fail-closed plus automatische Klassifizierung ist eine Falle.** Ein unsicherer Klassifikator sperrt im Zweifel jeden Cloud-Weg. Klassifikation ist Deklaration mit Untergrenze, keine Inhaltsanalyse.
6. **Eine Datei trägt das nicht.** Vor dem Ausbau der Schnitt in ein Paket.

## Stufen

| Stufe | Inhalt | Zweck |
|---|---|---|
| **0 Fundament** | Paket `ollama_router/` (common, state, config, tunnel, nodes, registry, poll, scheduler, proxy, openai_api, perf, wol, auth, ha, admin, app); `router.py` bleibt Einstieg; kanonisches Request-Modell und Adapter-Grenze folgen in Stufe 1 | kein neues Verhalten, Tests unverändert grün, ein Kontroll-Deploy |
| **2 Client-Identität** | Bearer-Token pro Client (Secret-Store), `allowedWorkloads`, `maxPriority`, Rate Limits, Request-Grössenlimits, Audit-Log | schliesst die offene Flanke auf 11434 |
| **1 Request-Semantik** | `routing`-Block in `/v1` und `/api`: required/preferred Capabilities, Requirements (`minContextTokens`, `jsonSchema`, `streaming`), `sessionId`, `requestId`; Routing-Info in der Antwort; Capabilities aus Ollama + Katalog-Overrides; `workload:x`-Alias | der Client beschreibt, der Router entscheidet |
| **3 Scheduler** | gewichtbarer Score, EWMA, Erfolgs-/Timeout-/Structured-Output-Raten, Circuit-Breaker-Zustände, Admission Control, Prioritätsklassen mit Aging, Deadline, Cancel bis zum Backend, Session Affinity, Residency-Regeln – Busy/free als expliziter Eingang | Qualität und Fairness |
| **4 Observability** | `/metrics` für Prometheus, Usage-Erfassung; OTel nur mit Collector | Nachvollziehbarkeit |
| **5 Cloud (Opt-in)** | Adapter (OpenAI, Anthropic, Gemini), `execution: cloud`, Datenklassen als Deklaration, Egress-Allowlist, Credential-Scan mit `BLOCK`, Budgets, Region | Ausnahme nach Hausregel |
| **6 Betrieb** | Canary, Shadow, Idempotency, Supply-Chain-Metadaten, Config-Schema | Reife |

Reihenfolge: **0 → 2 → 1 → 3 → 4 → 5 → 6.** Stand und Abweichungen werden hier fortgeschrieben. Stand 2026-09-10 abends: alle sechs Stufen sind gebaut und live, die Web-UI ist auf alle Stufen ausgebaut (README Web-UI). Was bleibt, ist Betrieb: Datenklasse je Client, Preise neuer Cloud-Modelle, Anthropic-Schlüssel. Nachtrag 2026-09-10 spät (Betreiber): fast alle Laufzeit-Einstellungen sind jetzt in der UI editierbar (Abschnitt Einstellungen, `settings.py`-Allowlist → `settings:`-Block in roles.yaml; Cloud-Budget, Client-Datenklassen, Scheduler, neue Cloud-Modelle im Katalog). Datenklasse je Client und Preise neuer Modelle sind damit UI-Sache, kein Deploy mehr.

## Stand

| Stufe | Stand | Anmerkungen |
|---|---|---|
| 0 Fundament | **erledigt 2026-09-09** | 16 Module, `tools/split_router.py`; 97 Tests unverändert grün, Kontroll-Deploy 20:48 |
| 2 Client-Identität | **erledigt 2026-09-10 (enforce live)** | Bearer / Basic / Quell-IP, Rollen- und Modellsperre, Rate Limit, Audit-Log, Laufzeit-Umschalter. Clients: `home-assistant` (Bearer), `node-red` (Bearer für `/v1` + IP für contrib-ollama, das keinen Header senden kann). Tokens in Secret-Store (ha-host, nodered-host). Live-Befund nach dem Deploy 22:09: Node-REDs contrib-ollama-Selbsttest wurde als `node-red via ip` erkannt (IP-Bindung funktioniert), der llm-config-Selbsttest als `bad_token` – dort stand noch der Platzhalter-Key vom Einrichten; kein Rückfall auf die IP. Tokens 22:23/22:25 verteilt (`nr_set_client_token.py`, `ha_set_api_key.py`; HA-Eintrag heisst jetzt **Skirnir**), danach beide Clients mit Identität: HA `bearer`, Node-RED `bearer` + `ip`. **2026-09-10 09:00 automatisch auf `mode: enforce` geschaltet** (`deploy/enforce_client_auth.py`, Aufgabe `skirnir-enforce-client-auth`): Baseline ohne neue Unbekannte, Proben ollama/llm/ha-conversation ok, dauerhaft in config.yaml. Nebenher am 2026-09-09: Name Skirnir durchgezogen (Repo `skirnir`, Secret-Store-Projekt `skirnir`, HA-Gerät), Router-WOL per Mitschnitt belegt und Register-Persistenz der Modelle nachgerüstet. Request-Grössenlimits bleiben bei `client_max_size` 64 MiB – feinere Limits (Bilder, Tools) in Stufe 1 mit dem Request-Modell. |
| 1 Request-Semantik | **erledigt 2026-09-10 (live 10:03)** | Modul `request.py`: `routing`-Block (require/prefer/min_context/session_id/request_id) nativ und in `/v1`; abgeleitete Anforderungen (tools, Bilder, think, format, suffix); Fähigkeiten aus `/api/show` + Katalog-Overrides (`capabilities: {structured: false}` für gpt-oss); unerfüllbar → 400 statt 503; Routing-Info als `X-Skirnir-*`-Header immer und als `routing`-Body nur bei gesendetem Block; `workload:x`-Alias; `router.limits` (413); Session-Affinität klein (§10, vorgezogen aus Stufe 3). 128 Tests grün. Abweichung: `/api/embed` leitet kein `embedding` ab (Ollama führt die Fähigkeit nur bei Embedding-Modellen). Nicht gebaut: `maxOutputTokens`/`maxImages` je Modell (Katalog) – kommt mit den Scheduler-Gewichten in Stufe 3, wenn es einen Nutzer gibt. |
| 3 Scheduler | **erledigt 2026-09-10** | Vorgezogen am Morgen: Residenz-Regel und Warm-Fit. Dann: gewichtbarer Score (`modes.score`), EWMA + Erfolgs-/Timeout-/Structured-Output-Raten je `model@node`, Circuit Breaker je Knoten (closed/open/half_open), Admission Control mit Prioritätsklassen interactive/normal/batch, Aging, Deadline (`routing.deadline_ms`), Client-Kappung `max_priority`, `max_inflight` je Knoten (Policy); Session-Affinität kam schon mit Stufe 1; Cancel bis zum Backend war vorhanden (Tunnel-CANCEL). Busy/free bleibt erster Eingang. README „Scheduler“. Nicht gebaut: `minimumLoadedTime`/`unloadCooldown`/`preferredWarmModels` (durch prewarm/keep_alive/Residenz abgedeckt), Katalog-Limits je Modell (kein Nutzer). 142 Tests grün. |
| 4 Observability | **erledigt 2026-09-10** | `metrics.py`: `GET /metrics` (Prometheus-Text ohne Zusatzbibliothek, Basic Auth wie /admin): Requests/Tokens-Counter mit role/node/model/client/via/outcome, Histogramme Dauer/TTFT/Wartezeit, Ereignis-Counter aus `state.remember`, Gauges für Knoten (Zustand, VRAM, GPU, Breaker, Inflight), Modelle, Rollen-Bereitschaft, Perf-EWMA, Client-Auth-Zähler. Usage je Tag/Client/Rolle/Modell in `usage.json` (90 Tage), `/admin/usage`, `usage_today` im State, HA-Sensoren Anfragen heute / Tokens heute. OTel bewusst nicht (kein Collector). Kein Prometheus im Haus: Endpunkt steht bereit. 147 Tests grün. |
| 5 Cloud | **erledigt 2026-09-10** | `cloud.py`: Anbieter als Katalogmodelle (`cloud: openai`) und Stufen in Rollen; Fälle 1–3 (kein Knoten, Fähigkeit fehlt, `execution: cloud`); Datenklassen als Deklaration (default personal, Cloud ≤ internal), Credential-Scan block, Budget je Anbieter/Monat in CHF mit HA-Warnung, Egress-Allowlist, Schlüssel aus Secret-Store, Breaker; Adapter OpenAI (auch Gemini-kompatibel) + Anthropic mit Stream/Tools/Bildern/format; Kosten in Usage, `/metrics`, HA-Sensor. Live: openai aktiv (Schlüssel aus HA übernommen), anthropic ohne Schlüssel deaktiviert (Abo ≠ API). Rollen: gross + gpt-5 als letzte Stufe, neue Rolle cloud. 174 Tests grün. Offen: Preise für gpt-5.4/5.5 im Katalog, Datenklasse je Client (Betreiber). |
| 6 Betrieb | **erledigt 2026-09-10** | `ops.py` + Config-Schema: `validate_schema` mit Vorschlägen, `router.py --check` (deploy.py prüft lokal vor dem Push und auf dem CT vor dem Neustart, fail-closed); Rollen-Merge roles.yaml/config.yaml behoben (assist hatte live `normal` statt `interactive`); Idempotency-Key für Nicht-Streams (TTL 600 s, Replay-Header, gleichzeitige Wiederholung wartet); Canary je Rolle (Anteil, auch kalt); Shadow je Rolle (nach der Antwort, nie parallel, Client `shadow` in Usage); Supply Chain (`build` aus deploy-Manifest mit SHA-256, `supply_chain` mit Ollama-Version und Modell-Digests je Knoten). 155 Tests grün. Nicht gebaut: Signaturen/SBOM. |

Abweichung von der Spezifikation in Stufe 2: **keine automatische Client-Erkennung über Requestparameter**, Identität
kommt ausschliesslich aus Header oder Quell-IP; `maxPriority` folgt erst mit den Prioritätsklassen in Stufe 3.

`mode: enforce` ist seit 2026-09-10 09:00 live (automatischer Umschaltvorgang, Proben ok, keine neuen Unbekannten).
