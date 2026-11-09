# Konfiguration

**Deutsch** · [English](configuration.md) · [README](../README.de.md)

## Dateien

| Datei | Zweck |
|---|---|
| `/etc/skirnir-router/config.yaml` | Die Konfiguration (0600). Ausgangspunkt ist [router/config.minimal.yaml](../router/config.minimal.yaml) (ein kleiner, sicherer Start: eine Rolle, ein Client; `router.py --init` füllt sie aus, siehe [install-router.de.md](install-router.de.md#erste-konfiguration)) oder [router/config.example.yaml](../router/config.example.yaml) (die volle Referenz mit jeder Option und ihrem Kommentar). |
| `/etc/skirnir-router/roles.yaml` | Overrides, die die Web-UI neben die `config.yaml` schreibt; überlebt Deploys und Aktualisierungen. |
| `/etc/skirnir-router/secrets.env` | Secrets als Umgebung ([install-router.de.md](install-router.de.md#secrets)). |

Rollen, Katalog und die meisten Laufzeit-Einstellungen sind in der UI editierbar; die UI schreibt sie in die `roles.yaml`.
Ports, TLS, Basic-Auth, Identitäten, Anbieter-Endpunkte und Egress-Allowlist bleiben bewusst Deploy-Sache. Katalogeinträge aus
beiden Dateien werden Feld für Feld zusammengeführt: Eine Messung in `roles.yaml` ersetzt die Größenangaben (Gewichte, KV,
Messwerte) als Einheit, andere Felder aus der `config.yaml` wie `max_parallel` oder Fähigkeits-Overrides gelten weiter.

Nach jeder Änderung: `python3 router.py --check /etc/skirnir-router/config.yaml` (unbekannte Schlüssel kommen mit Vorschlag;
`--pure` ignoriert die `roles.yaml`) und `python3 router.py --posture /etc/skirnir-router/config.yaml`.

## Client-Identitäten auf dem Inferenz-Port

`router.client_auth` kennt die Clients und führt sie in zwei Phasen ein: `mode: observe` bedient alles, zählt Unbekannte und
schreibt sie ins Audit-Log; `mode: enforce` antwortet ohne gültige Identität mit 401 (Ollama-, OpenAI- oder Anthropic-Fehlerformat).
`/` und `/api/version` bleiben frei. `locked: true` verhindert, dass der Modus über UI oder API geändert wird.

Drei Wege, in dieser Reihenfolge: `Authorization: Bearer <token>` (so schickt die HA-Ollama-Integration ihren API-Key, OpenAI-
Clients ebenso; Anthropic-Clients dürfen das Token stattdessen als `x-api-key` schicken), `Authorization: Basic <client>:<token>`
und **Quell-IP** für Clients, die keinen Header senden können (z. B. `node-red-contrib-ollama`, das seinen Key nur an ollama.com
schickt). Ein falsches Token fällt nicht auf die IP zurück, es zählt als `bad_token`. Tokens sind 256 Bit Zufall, gespeichert
wird nur ihr **sha256** – die Prüfung läuft bei jeder Anfrage, PBKDF2 wäre hier Selbstsabotage.

```yaml
client_auth:
  mode: enforce
  locked: true
  audit_log: /var/log/skirnir-router/audit.jsonl
  clients:
    home-assistant: { token_sha256: "…", roles: ["*"], models: true, requests_per_minute: 120 }
    node-red:       { token_sha256: "…", ip: ["192.0.2.20"], roles: ["*"], models: true }
    batch-jobs:     { token_sha256: "…", roles: ["gross"], models: false, max_priority: batch, cloud: false, data_class: internal }
```

| Je Client | Bedeutung |
|---|---|
| `roles` | Rollen, die der Client nutzen darf (`["*"]` oder fehlend = alle) |
| `models` | Konkrete Modellnamen erlaubt? ([ADR-0052](adr/0052-concrete-model-names-need-an-explicit-grant.md)) |
| `requests_per_minute` | Rate Limit; darüber 429 |
| `max_concurrent` | Gleichzeitig laufende Anfragen, Standard 4; die nächste bekommt 429 mit `Retry-After: 1` ([ADR-0063](adr/0063-bound-concurrency-and-body-size-per-client.md)) |
| `max_body_mib` | Angegebene Body-Größe, Standard 16; darüber 413 (ADR-0063) |
| `max_priority` | Höchste Admission-Priorität, die der Client beanspruchen darf |
| `cloud`, `data_class` | Cloud-Stufen erlaubt, deklarierte Datenklasse ([unten](#cloud-als-stufe-in-rollen)) |
| `privacy_check` | Inhaltsprüfung vor einer Cloud-Stufe (unten) |
| `rerank` | Recht auf den [Rerank-Endpunkt](reference.de.md#optionaler-rerank-endpunkt) |
| `inference` | `false` = nur der Rerank-Endpunkt, alles andere 403 |
| `ip` | Quelladressen für Clients ohne Header |

Abweisungen landen im Audit-Log, nie mit Prompts oder Tokens: `auth_denied` (keine gültige Identität unter `enforce`, 401;
unter `observe` als `auth_missing` protokolliert), `bad_token`, `forbidden` (Rolle, konkretes Modell oder Inferenz nicht
erlaubt, 403), `rate_limited` und `concurrency_limited` (429), `body_too_large` (413). Clients lassen sich auch in der UI anlegen;
der Router erzeugt das Secret, zeigt den Klartext genau einmal und speichert den Hash in `roles.yaml`. Der Knopf „Ausprobieren“
nutzt die interne Identität `skirnir-ui`, die nur von localhost gilt.

## Rollen und Stufen

```yaml
roles:
  standard:
    exposed_as: standard:latest
    priority: normal                      # interactive | normal | batch (Admission)
    tiers:
      - { model: qwen3.6:35b-a3b, num_ctx: 65536 }
      - { model: qwen3.6:35b-a3b, num_ctx: 32768 }
      - { model: gemma4:12b,      num_ctx: 65536 }            # Ausweichstufe auf dem zweiten Knoten
      - { model: granite4.2:8b,   num_ctx: 32768, busy_ok: true }   # darf auf einer spielenden GPU laufen
  assist:
    priority: interactive
    tiers: [...]
```

- **Rangliste = Qualitätsobergrenze und Kaltstart-Reihenfolge.** Ist ein Listenmodell auf irgendeinem Knoten schon geladen,
  gewinnt es (unter mehreren warmen das ranghöchste). Global `modes.warm_first`, je Rolle `latency_first`.
- **Kontextleiter.** Dasselbe Modell mit kleinerem `num_ctx` ist die nächste Stufe, wenn der Kontext nicht ins VRAM passt.
  Passt-Prüfung: `VRAM-Bedarf = Gewichte + Kontext-Kosten × num_ctx/1000 + 0,8 GiB`. Gewichte und Kontext-Kosten je Modell
  stehen im Katalog (`models:`), gemessen per UI-Knopf **Messen** (lädt bei 8k und 32k, liest `/api/ps`) oder geschätzt.
  Anhaltspunkte: [operations.de.md](operations.de.md#messwerte-als-anhaltspunkt).
- **Busy.** Ein Knoten ist `busy`, wenn fremdes VRAM (belegt − Ollama − Desktop-Grundverbrauch) über der Schwelle liegt oder die
  GPU ohne Router-Anfragen ausgelastet ist. Dann laufen nur `busy_ok`-Stufen; nicht-`busy_ok`-Modelle werden alle 30 s entladen.
  Der Desktop-Grundverbrauch wird je Knoten gelernt (Median der Stundenmittel bei ruhiger GPU) und durch einen Policy-Wert gedeckelt.
  `busy` zu verlassen ist schwerer als hineinzukommen (Hysterese): Der Knoten bleibt busy, solange die Auslastung über
  `busy_exit.gpu_util_pct` (20 %) liegt und fremdes VRAM belegt ist, und wird erst nach `below_for_s` Ruhe wieder free - ein
  leichtes Spiel um die Eintrittsschwelle schaltete den Knoten sonst alle paar Sekunden um.
- **Spiel und Hauptspeicher als Fakt (Agent 0.17.0, Windows).** Der Agent meldet, ob ein Spiel läuft (Prozesspfad in einer
  Steam-Bibliothek oder im Installationsordner eines bekannten Spieleverlags; ist der Pfad nicht lesbar, über den Namen
  eines großen Programms in so einem Ordner), und den freien Hauptspeicher (verfügbarer RAM und freier Commit). Ein
  laufendes Spiel macht den Knoten nach `busy_enter.game_sustain_s` (10 s) `busy`, auch wenn es kaum VRAM belegt, und hält
  ihn nach dem Ende noch `busy_exit.game_quiet_s` (60 s). Seit Router 0.8.7 / Agent 0.19.0 gilt das nur für ein Spiel, das die
  GPU braucht: Der Agent meldet auch die GPU-Nutzung des Spiels selbst (dedizierter und gemeinsamer Grafikspeicher, höchste
  Engine-Last, über alle Spielprozesse), und das Spiel zählt, sobald es `busy_enter.game_min_gpu_gib` (2 GiB) oder
  `game_min_gpu_pct` (10 %) erreicht oder während Speicherdruck herrscht; ab dann hält es den Knoten bis zu seinem Ende busy.
  Gemeinsamer Speicher zählt, weil ein Spiel, das neben einem großen Modell kein VRAM bekommt, dorthin ausweicht. Ein leichtes
  Spiel (gemessen: 735 MiB, 2 %) lässt den Knoten frei, und das Vorwärmen läuft weiter; ein Vorwärmen, das ausgelassen wurde,
  während das Spiel noch beurteilt wurde, läuft, sobald es als leicht gilt oder beendet ist (0.8.8). Ein Agent, der keine
  GPU-Nutzung meldet, bekommt das alte Verhalten. `game_heavy` und sein Grund stehen in `/admin/state`.
  Liegen verfügbarer RAM oder freier Commit `memory_pressure.polls` Heartbeats lang (2) unter `memory_pressure.min_free_gib`
  (8 GiB), steht der Knoten unter Speicherdruck: nichts wird kalt geladen, kein Prewarm; geladene Modelle antworten weiter,
  entladen wird nichts. Grund-Code `memory_pressure` mit `Retry-After` 60 s. `skirnir-agent game` zeigt, was der Agent erkennt.
- **Prewarm.** Nach dem Online-Gehen und nach `busy → free` lädt der Router die Rang-1-Modelle der Rollen wieder vor, so viele
  zusammen ins VRAM passen. Eine **Residenz-Regel** holt ein verdrängtes Rang-1-Modell zurück, wenn der Verdränger 5 min nicht
  mehr gefragt wurde – sonst bliebe „warm zuerst“ dauerhaft auf der Ausweichstufe hängen.
- **Konkrete Modelle** (`qwen3-coder:30b`) sind weiter direkt aufrufbar (`expose_concrete_models`) und gehen 1:1 an einen Knoten,
  der sie hat; sonst 404 wie bei Ollama. Ein Client braucht dafür `models: true`; ohne die Einstellung bekommt er nur Rollen,
  und der Router warnt beim Laden (ADR-0052).
- **Canary** (`roles.<r>.canary: {model, num_ctx, percent}`) leitet einen Anteil des Verkehrs auf einen Kandidaten, auch kalt.
  **Shadow** (`roles.<r>.shadow`) wiederholt einen Anteil der Anfragen nach der Antwort ohne Stream gegen ein zweites Modell auf
  einem freien Knoten; der Client merkt nichts, Tempo und Fehlerrate landen in der Statistik.

## Scheduler, Circuit Breaker, Admission

- **Score** unter den Kandidaten einer Stufe: warm +100, je laufende Anfrage −10, voller Knoten −50, freies VRAM (Anteil) ×2,
  Gewicht ×1, Tempo (EWMA tok/s / 100) ×2, Fehlerrate der letzten 20 Ergebnisse ×20, Breaker-Probe −5, Umschalten auf ein
  Ersatz-Ollama −60 ([reference.de.md](reference.de.md#optionaler-openai-kompatibler-knoten)). Gewichte in `modes.score`
  (Schlüssel `warm`, `inflight`, `saturated`, `vram_free`, `weight`, `speed`, `errors`, `half_open`, `switch`), UI-editierbar;
  nur Gewichte eintragen, die vom Standard abweichen.
- **Statistik je `model@node`:** Mittel der letzten 20 warmen Läufe und EWMA (α 0,3) für tok/s und Zeit bis zum ersten Token,
  Ergebnisse ok / error / timeout / structured_error. Persistiert in `perf.json`.
- **Circuit Breaker je Knoten** (`modes.breaker`): drei Backend-Fehler in 60 s → `open` (30 s), dann `half_open` mit genau
  einer Probe. Ein Neustart des Knotens setzt ihn zurück. Retry auf einen anderen Knoten bei Verbindungsfehler oder 5xx vor dem
  ersten Byte.
- **Modellwechsel:** Lädt ein Knoten gerade ein Modell, warten weitere Anfragen, die diesen Knoten bräuchten, auf das
  Ende des Ladens statt 503 zu bekommen (Ereignis `wait_load`).
- **Admission** (`modes.admission`): höchstens `max_inflight` gleichzeitige Anfragen je Knoten (Policy im Register, Vorgabe 2 –
  sollte `OLLAMA_NUM_PARALLEL` des Knotens entsprechen). Weitere warten im Router; beim Freiwerden gewinnt der beste Rang aus
  Prioritätsklasse und Alter (`aging_s` 30 s, damit Batch nicht verhungert). `max_queue` überschritten → 503 sofort.
- **Modelle ohne Parallelbetrieb:** `models.<m>.max_parallel` begrenzt die gleichzeitigen Anfragen je Knoten für ein Modell.
  qwen3.8 (Architektur qwen35) kann in Ollama keine zwei Anfragen zugleich; eine trotzdem geschickte zweite wartet in Ollamas
  eigener Schlange, und Ollama lädt den Runner neu, sobald die erste fertig ist (5–7 s, Kontext-Checkpoints und Prompt-Cache
  weg). Mit `max_parallel: 1` wartet die zweite Anfrage stattdessen im Router, mit Priorität, und der Runner bleibt.
- **Wartebudget je Prioritätsklasse**, wenn die Anfrage kein `deadline_ms` setzt: `max_wait_interactive_s` (60 s; ein
  Sprachbefehl nach einer Minute ist ohnehin verloren), `max_wait_s` für normal (600 s; Agenten-Anfragen dauern oft Minuten)
  und `max_wait_batch_s` (1800 s).
- **Interaktive Anfragen warten nicht hinter einem belegten warmen Modell**, wenn eine andere lokale Stufe sofort bedienen
  kann: zuerst eine freie warme, dann eine freie kalte Stufe, andere Knoten vor dem belegten (nie eine Cloud-Stufe auf
  diesem Weg); im Entscheidungslog steht `reason: fallback`. Ist nichts Warmes belegt, entscheidet warm zuerst wie
  bisher; ist keine Stufe frei, wartet die Anfrage innerhalb ihres interactive-Budgets.
- **Beobachtungsmodus** (`decision_engine.observe_blocked`): Muss eine Anfrage an eine Rolle warten, ordnet die Decision Engine
  sie im Hintergrund ein, nur mit ihrer CPU-Kette (Embedding, TF-IDF, Regeln; nie das LLM, das sich auf genau der GPU
  anstellen würde, auf die gewartet wird), und das Entscheidungslog hält `decision_observe` fest: Klasse, Sicherheit, die
  erste freie tiefere Stufe derselben Rolle und `would_fallback` für die Klassen in `observe_light`. Am Routing ändert sich
  nichts; es soll erst messen, wie oft ein freies kleineres Modell gereicht hätte, bevor der Router danach handelt.
  `observe_all` geht weiter: Jede Anfrage an `/api/chat` oder `/api/generate` wird beim Start im Hintergrund eingeordnet, und
  an ihrem Ende hängt der Router eine Zeile an `requests.jsonl` neben der Konfiguration (`observe_path`): Klasse, Dauer,
  Wartezeit, Token, Denken, Zahl der Werkzeuge, Client, Rolle, Ergebnis - keine Prompt-Inhalte. Eigene Datei, damit das
  Entscheidungslog sein Fenster behält; sie rotiert bei 20 MB. Die Antwort wartet nie auf die Einordnung.

## Auto-Rolle: die Decision Engine

Ein Client, der nicht weiß, welche Rolle passt, fragt `auto:latest`. Eine Kette von Engines wählt eine der konfigurierten
Rollen; unsicher (kleiner Abstand Platz 1/2, hohe Entropie, geringe Wahrscheinlichkeit) heißt: nächste Engine, am Ende `default`.
Entwurf: [design/decision-engine.md](../design/decision-engine.md), Messreihe: [decision-eval/README.md](../decision-eval/README.md).

| Engine | Was | Top-1 auf 132 ungesehenen Testfällen | Latenz | Wo |
|---|---|---|---|---|
| `embed` | `multilingual-e5-small` als ONNX int8 (113 MB) + Softmax-Kopf, eigener Container ([decision-embed/](../decision-embed/)) | **0,932** | 10–19 ms | CPU, 4 Kerne, 456 MiB |
| `local_llm` | kleines Modell über den Router selbst mit erzwungenem JSON-Schema | **0,977** | ~200 ms warm | GPU-Knoten |
| `tfidf` | Zeichen-n-Gramme + Softmax-Regression, 570 KB JSON im Prozess ([decision-eval/train_tfidf.py](../decision-eval/train_tfidf.py)) | 0,848 | < 1 ms | im Router |
| `rules` | Stichwörter, deterministisch, Kette-Ende | 0,523 | 0,1 ms | im Router |
| `jevlike` | [Jevlike](https://github.com/vinnylarouge/jevlike)-Adapter ([decision-jevlike/](../decision-jevlike/)); gebaut, gemessen, für feste Rollen nicht lohnend | 0,750 | 15 ms | eigener Dienst |

Die Tabelle ist eine einzige Aufteilung des früheren Datensatzes (2026-09-17). Neu trainiert auf den neutralen Daten
(Router 0.25.1, ADR-0077) und über 50 Aufteilungen der Vorlagen gemessen: tfidf 0,91 ± 0,06, embed 0,96 ± 0,03, im
Rahmen der Streuung gleich wie vorher; eine einzelne Aufteilung verschiebt Top-1 allein um mehrere Punkte
([design/decision-training-data.md](../design/decision-training-data.md)).

Empfohlene Kette `[embed, local_llm, tfidf, rules]`: embed entscheidet in unter 20 ms, bei Unsicherheit (~6 % der Fälle) fragt
der Router das LLM, fällt der GPU-Knoten aus, greifen tfidf und rules. Routing-Genauigkeit der Kette 0,947; rein auf CPU
(`[embed, tfidf, rules]`) 0,909.

```yaml
decision_engine:
  enabled: true
  role: auto                                   # erscheint als auto:latest in /api/tags
  options: [standard, gross, assist, code]
  default: standard
  chain: [embed, local_llm, tfidf, rules]
  policy: { min_top_probability: 0.5, min_margin: 0.2, max_entropy_ratio: 0.75 }
  tfidf: { model_path: /etc/skirnir-router/decision-tfidf.json }
  embed: { endpoint: http://127.0.0.1:8082, timeout_s: 2 }
  local_llm: { model: "assist:latest", timeout_s: 20, descriptions: { code: "Programmieren, Skripte, Fehlersuche", ... } }
  capture: { enabled: false, path: /var/lib/skirnir-router/decisions.jsonl, clients: [], anonymize: true }
```

`config.example.yaml` liefert diesen Block eingeschaltet mit der Kette `[embed, local_llm, tfidf, rules]`. tfidf-Datei und
embed-Container sind optional ([install-router.de.md](install-router.de.md#optional-decision-engine-für-die-auto-rolle)): Lässt
sich die tfidf-Datei nicht laden, loggt der Router beim Start eine Warnung und lässt die Engine weg; antwortet der
embed-Container nicht, loggt jede Entscheidung eine Warnung und geht mit der nächsten Engine weiter. Ohne beides
`enabled: false` setzen oder eine kürzere Kette wie `[local_llm, rules]`. `local_llm` braucht `descriptions` für die Rollen;
die Namen allein sagen dem Modell nichts.

Das Ergebnis steht in `routing.decision` (Verteilung, Engine, Latenz, Unsicherheitsgründe, Fallback-Spur), in den Metriken
`skirnir_decision_*` und im Entscheidungsprotokoll. `POST /admin/decide` fragt eine Engine direkt, `GET /admin/decision` zeigt
Kette, Policy und Gesundheit. **Capture** schreibt Trainingsdaten (JSONL) nur für Clients im Opt-in, anonymisiert (Schlüssel,
E-Mail, IP, URL, lange Zahlen) und mit Gruppen-Hash; echte Rollenwahlen der Clients (`client`) bleiben von Engine-Pseudolabels
(`engine`) getrennt. Der Embedding-Kopf trainiert daraus in Sekunden neu; das Embedding-Modell bleibt.

## Cloud als Stufe in Rollen

Cloud-Anbieter sind **Modelle im Katalog** (`"openai:gpt-5-mini": {cloud: openai, provider_model: gpt-5-mini, price_chf_per_m: {...}}`)
und liegen als Stufe in genau den Rollen, die es dürfen. Konkrete Cloud-Modellnamen sind nicht direkt aufrufbar (404), nur über
Rollen. Ein kaltes lokales Modell gewinnt vor der Cloud. Drei Fälle, in denen die Cloud zum Zug kommt: kein lokaler Knoten kann
(alle spielen oder aus), eine Fähigkeit fehlt lokal, oder der Client verlangt es (`routing.execution: cloud`).

Schranken (`router.cloud`):

- **Datenklassen als Deklaration**, kein Inhaltsklassifikator: `routing.data_class`, sonst `clients.<c>.data_class`, sonst
  `default_data_class` (**personal**). Cloud nur bis `max_cloud_data_class` (**internal**). Folge: wer nichts deklariert, geht nie
  in die Cloud.
- **Inhaltsprüfung statt Pauschalsperre** (Router 0.13.0, Opt-in je Client: `clients.<c>.privacy_check: true` und
  `router.cloud.privacy_check.enabled: true`): Für einen Client, dessen Klasse ohne Deklaration `personal` ist, bleibt die
  Cloud-Stufe Kandidat, und wenn sie wirklich gewählt wird, wird die Anfrage geprüft: Regeln auf dem, was der Nutzer
  geschrieben hat (eigene `terms` wie Namen der Familie, E-Mail, Telefon, IBAN, AHV, Straßenadresse, eingebaute
  Stichwortgruppen für Gesundheit, Finanzen, Zugangsdaten, Beziehungen, Recht), danach ein Klassifikator auf dem Router-Host
  (decision-embed `POST /privacy`, e5-small mit trainiertem Kopf, wenige ms). Über seiner Schwelle, bei einem Regeltreffer
  oder wenn der Klassifikator nicht antwortet (fail closed), bleibt die Anfrage lokal und bekommt den eingestellten Satz
  `refusal` als Antwort. Bevor eine erlaubte Anfrage hinausgeht, werden Werkzeugergebnisse geschwärzt: Einträge des
  Home-Assistant-Live-Kontexts aus `redact_domains` (person, device_tracker, todo, lock, camera, …),
  `redact_device_classes` (motion, presence, door, window, …) und `redact_names`, und das ganze Ergebnis der `redact_tools`.
  Das Journal hält `privacy_check` mit Ergebnis, Regel oder Wahrscheinlichkeit und Anzahl geschwärzter Einträge fest, nie
  den Text. Eine deklarierte `routing.data_class` wird nie übergangen.
- **Credential-Scan** (`block`): Prompts mit erkennbaren Schlüsseln (`sk-…`, `AKIA…`, `ghp_…`, JWT, `PRIVATE KEY`, `password: …`)
  gehen nicht in die Cloud; lokal laufen sie normal.
- **Budget** je Anbieter und Monat, in der Währung der Katalogpreise (der Router rechnet keine Währungen um; die Endung
  `_chf` von Schlüsseln, Feldern und Metriken und die Einheit des HA-Kostensensors sind historisch); erschöpft =
  Anbieter fällt als Stufe weg, ab `warn_at_percent` ein HA-Problem. Kosten je Anfrage in Usage und Metriken.
- **Egress-Allowlist** für `base_url` (+ `egress_allow`), Schlüssel nur aus `secrets.env`, nie im Log; Circuit Breaker wie bei
  Knoten; Schattenläufe gehen nie in die Cloud.
- **Transport:** Anbieter benötigen den endgültigen HTTPS-Endpunkt. HTTP ist nur für explizit erlaubte, wörtliche
  Loopback-Adressen (127.0.0.1 und ::1) möglich. Weiterleitungen des Anbieters führen zu einem Upstream-Fehler.

Adapter: `openai` (auch für OpenAI-kompatible Endpunkte) und `anthropic` (Messages-API). Beide übersetzen in das Ollama-Format,
danach gelten dieselben Wege wie bei Knoten.
