# Konfiguration

**Deutsch** · [English](configuration.md) · [README](../README.de.md)

## Dateien

| Datei | Zweck |
|---|---|
| `/etc/skirnir-router/config.yaml` | Die Konfiguration (root, 0640: der Router liest sie über seine Gruppe `skirnir-router`, ADR-0086). Ausgangspunkt ist [router/config.minimal.yaml](../router/config.minimal.yaml) (ein kleiner, sicherer Start: eine Rolle, ein Client; `router.py --init` füllt sie aus, siehe [install-router.de.md](install-router.de.md#erste-konfiguration)) oder [router/config.example.yaml](../router/config.example.yaml) (die volle Referenz mit jeder Option und ihrem Kommentar). |
| `/var/lib/skirnir-router/roles.yaml` | Overrides, die die Web-UI in den Zustandsordner des Routers schreibt (`router.state_dir`); überlebt Deploys und Aktualisierungen. Der Router schreibt alle eigenen Dateien dorthin - `roles.yaml`, `nodes.json`, Journale, Metriken, einen SQLite-Session-Store, den eine relative `sqlite:///`-URL nennt (ADR-0114) - und nichts in `/etc/skirnir-router` (ADR-0086). Ohne `state_dir` liegen sie neben der `config.yaml`, was nur für die Entwicklung taugt: das Konto des Dienstes kann `/etc/skirnir-router` nicht schreiben. |
| `/etc/skirnir-router/secrets.env` | Secrets als Umgebung (root, 0640 für die Gruppe des Routers; [install-router.de.md](install-router.de.md#secrets)). |

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
| `max_body_mib` | Body-Größe, Standard 16; darüber 413: eine angegebene Größe vor jeder Arbeit, ein Chunked-Body beim Lesen (ADR-0063, ADR-0129) |
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
    options: { temperature: 0 }           # Sampling-Vorgaben für die lokalen Stufen (ADR-0093)
    system_append:                        # Text nur für das Modell, das antwortet (ADR-0094)
      granite4.2:8b: "Antworte in höchstens zwei Sätzen."
    tiers: [...]
```

- **Sampling-Vorgaben je Rolle** (`roles.<r>.options`, Router 0.30.0, [ADR-0093](adr/0093-sampling-defaults-per-role.md)).
  Schlüssel: `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `frequency_penalty`, `repeat_penalty`
  (Ollama-Namen). Der Router ergänzt sie in einer Anfrage an die Rolle, wenn der Client sie weglässt; ein Wert des Clients
  gewinnt. Sie gelten nur für die lokalen Stufen der Rolle: Ollama-Knoten bekommen sie als Optionen, OpenAI-kompatible
  Knoten (llama.cpp, Strata) als Felder der Anfrage (`repeat_penalty` als `repetition_penalty`); eine Cloud-Stufe bekommt
  sie nie, weil manche Cloud-Modelle eine andere als ihre Standard-Temperatur ablehnen. Eine Anfrage für ein konkretes
  Modell bekommt keine. Unbekannte Schlüssel, falsche Typen und Werte außerhalb des Bereichs verhindern das Laden der
  Konfiguration. Wie `priority` gehören sie in `config.yaml`: Eine in der Web-UI gespeicherte Rolle trägt sie nicht mit.
- **Systemtext je Modell** (`roles.<r>.system_append`, Router 0.31.0, [ADR-0094](adr/0094-system-text-per-model-of-a-role.md)).
  Ordnet Katalogmodellen einen Text zu (je höchstens 8000 Zeichen). Bedient ein Modell mit Eintrag eine Chat-Anfrage
  an die Rolle, hängt der Router seinen Text an die führende Systemnachricht (oder schickt ihn als neue führende
  Systemnachricht); jede Stufe mit diesem Modell bekommt ihn, auch ein Canary, kein anderes Modell. Gedacht für
  Prompt-Regeln, die ein Modell braucht und ein anderes nicht. `/api/generate`, Embeddings und Anfragen für ein
  konkretes Modell bleiben unverändert. Der Routen-Eintrag im Journal hält die angehängte Länge fest, nicht den Text.
  Ein Modell, das im Katalog fehlt, ein Nicht-Text oder ein zu langer Text verhindern das Laden der Konfiguration.
  Wie `options` gehört es in `config.yaml`; eine in der Web-UI gespeicherte Rolle behält es. Der Text erreicht eine
  Cloud-Stufe wie der Systemprompt des Clients und wird von der Datenschutzprüfung nicht eingestuft, darf also keine
  persönlichen Daten enthalten.
- **Rangliste = Qualitätsobergrenze und Kaltstart-Reihenfolge.** Ist ein Listenmodell auf irgendeinem Knoten schon geladen,
  gewinnt es (unter mehreren warmen das ranghöchste). Global `modes.warm_first`, je Rolle `latency_first`.
- **Kontextleiter.** Dasselbe Modell mit kleinerem `num_ctx` ist die nächste Stufe, wenn der Kontext nicht ins VRAM passt.
  Passt-Prüfung: `VRAM-Bedarf = Gewichte + Kontext-Kosten × num_ctx/1000 + 0,8 GiB`. Gewichte und Kontext-Kosten je Modell
  stehen im Katalog (`models:`), gemessen per UI-Knopf **Messen** (lädt bei 8k und 32k, liest `/api/ps`) oder geschätzt.
  Anhaltspunkte: [operations.de.md](operations.de.md#messwerte-als-anhaltspunkt).
- **Benchmark.** Der UI-Knopf **Benchmark** misst Erzeugungs- und Prompt-Tempo eines geladenen Modells (ein Aufwärmlauf, zwei
  Läufe, 200 Tokens, Temperatur 0) mit `router.benchmark.prompt` (Router 0.37.0,
  [ADR-0106](adr/0106-language-dependent-defaults-live-in-the-configuration.md)). Vorgabe ist der deutsche Prompt aller
  bisherigen Messungen, unverändert. Tokens pro Sekunde hängen von der Sprache von Prompt und Antwort ab (Tokenizer zerlegen
  Sprachen in unterschiedlich viele Tokens); setze also einen Prompt in der Sprache deiner Clients und vergleiche nur
  Ergebnisse mit derselben `prompt_id`, die jedes Ergebnis festhält.

  ```yaml
  router:
    benchmark:
      prompt: "Explain in about 200 words what a network router does and how it differs from a switch."
  ```
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
  Seit Agent 0.36.0 schaltet `game.enabled: false` in der Agent-Konfiguration die Erkennung ab (ADR-0103); der Agent meldet
  dann `enabled: false`, und der Router kennt für den Knoten kein Spiel. Ein Knoten ohne GPU (`gpu_present: false`,
  ADR-0102) hat keine GPU-busy-Erkennung; der Router bemisst seine Modelle nach den Speicherfakten unten.
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
  Ende des Ladens statt 503 zu bekommen (Ereignis `wait_load`). Das gilt auch vor einer kalten Wahl: Lädt gerade eine
  lokale Stufe, die vor der gewählten steht, ihr eigenes Modell, wartet die Anfrage einmal auf dieses Laden (höchstens
  bis zu ihrer Admission-Frist), statt in die Cloud zu gehen (ADR-0095) oder daneben ein tieferes Modell zu laden
  (ADR-0096). Eine tiefere Stufe, die schon geladen ist, wird weiterhin sofort genommen.
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
  diesem Weg); im Entscheidungslog steht `reason: fallback`. Eine kalte Stufe auf dem belegten Knoten kommt nur in
  Frage, wenn sie neben dem antwortenden Modell Platz hat; eine, die dessen Speicher bräuchte, würde es verdrängen, sobald
  seine Anfrage endet (ADR-0097). Ist nichts Warmes belegt, entscheidet warm zuerst wie bisher; ist keine Stufe frei,
  wartet die Anfrage innerhalb ihres interactive-Budgets.
- **Beobachtungsmodus** (`decision_engine.observe_blocked`): Muss eine Anfrage an eine Rolle warten, ordnet die Decision Engine
  sie im Hintergrund ein, nur mit ihrer CPU-Kette (Embedding, TF-IDF, Regeln; nie das LLM, das sich auf genau der GPU
  anstellen würde, auf die gewartet wird), und das Entscheidungslog hält `decision_observe` fest: Klasse, Sicherheit, die
  erste freie tiefere Stufe derselben Rolle und `would_fallback` für die Klassen in `observe_light`. Am Routing ändert sich
  nichts; es soll erst messen, wie oft ein freies kleineres Modell gereicht hätte, bevor der Router danach handelt.
  Ohne `observe_light` setzt die Konfigurationsprüfung `[standard, assist]`, beschränkt auf die Namen unter `options` (Router
  0.37.0, [ADR-0106](adr/0106-language-dependent-defaults-live-in-the-configuration.md)); heißen die Rollen anders, nenne
  die leichten Klassen selbst. Eine angegebene Liste gilt wie angegeben, `[]` heißt: keine Klasse gilt als leicht.
  `observe_all` geht weiter: Jede Anfrage an `/api/chat` oder `/api/generate` wird beim Start im Hintergrund eingeordnet, und
  an ihrem Ende hängt der Router eine Zeile an `requests.jsonl` neben der Konfiguration (`observe_path`): Klasse, Dauer,
  Wartezeit, Token, Denken, Zahl der Werkzeuge, Client, Rolle, Ergebnis - keine Prompt-Inhalte. Eigene Datei, damit das
  Entscheidungslog sein Fenster behält; sie rotiert bei 20 MB. Die Antwort wartet nie auf die Einordnung.

## Knoten wecken

Bleibt für eine Anfrage kein Kandidat übrig, darf der Router einen offline Knoten wecken, der das Modell kannte, und die
Anfrage danach neu verteilen (Router 0.41.0, ADR-0117). Mit dabei ist ein Knoten, dessen Policy oder statischer Eintrag
`wol: true` sagt (der Name stammt aus der Zeit, als es nur Wake-on-LAN gab); `waker` sagt, wie. Das Warten ist für jeden
Wecker gleich: Der Router fragt den Knoten alle 2 s ab, höchstens `router.wol.wait_up_s` (90 s) lang, und versucht
denselben Knoten erst nach `router.wol.cooldown_s` (300 s) wieder. Meldet ein Wecker *gesendet*, heisst das nur, dass das
Signal den Router verlassen hat; ob der Knoten hochkommt, entscheidet die Abfrage.

| Wecker | Was er tut | Braucht |
|---|---|---|
| `wol` (eingebaut, Standard) | UDP-Magic-Packet an jede Adresse in `router.wol.broadcast_addresses`, Port 9, während des Wartens alle `retry_interval_s` erneut | die MAC des Knotens (vom Agenten oder aus der Policy), Router in derselben Broadcast-Domäne |
| `http_webhook` | je Versuch ein `POST` mit dem festen JSON-Körper `{"event": "wake", "node": "<name>", "mac": "<mac oder null>"}` | eine konfigurierte endgültige URL; optional ein Bearer-Token |

```yaml
router:
  wakers:
    plug:                                  # beliebiger Name ausser wol
      kind: http_webhook
      url: https://automation.example.net/api/webhook/skirnir-wake
      token_env: SKIRNIR_WAKE_TOKEN        # optional; das Token kommt aus der Umgebung (secrets.env)
      timeout_s: 10                        # ganzer Aufruf, 1-60 s
      repeat: false                        # true = alle router.wol.retry_interval_s erneut senden
nodes:
  pc-example: { wol: true, waker: plug }   # ein registrierter Knoten nimmt das als Grundlage; der Policy-Schlüssel waker überschreibt es
```

- **Wohin der Webhook geht:** ein Home-Assistant-Webhook-Auslöser (`/api/webhook/<id>`), der einen Smart-Plug schaltet oder
  `wake_on_lan.send_magic_packet` in einem anderen Subnetz aufruft, eine ioBroker- oder openHAB-Regel hinter einem
  HTTP-Endpunkt oder eine eigene Steuerung. Der Körper nennt den Knoten, ein Endpunkt kann also mehrere Knoten bedienen.
- **Transportregeln** (ADR-0124, dieselben wie für Cloud-Anbieter in ADR-0048): HTTPS, einfaches HTTP nur an das wörtliche
  `127.0.0.1` oder `::1`; keine Zugangsdaten, keine Query und kein Fragment in der URL; eine Umleitung wird abgelehnt, nicht
  befolgt (die endgültige URL eintragen); `timeout_s` begrenzt den ganzen Aufruf, und ein langsamer Empfänger verzögert nur die
  Anfrage, die auf diesen Knoten wartet. Das Token geht als `Authorization: Bearer ...` mit; ist `token_env` gesetzt, die
  Variable aber leer, wird nichts gesendet. Das Log nennt nur den Host des Empfängers, nie den URL-Pfad (eine
  Home-Assistant-Webhook-ID ist ein Geheimnis).
- **`repeat`** bleibt aus bei allem, was umschaltet: ein zweites „Einschaltknopf drücken“ würde den Rechner wieder
  ausschalten. Für gleichbleibende Aufrufe („Steckdose ein“) einschalten, wenn der Empfänger den ersten verpassen kann.
- **Wahl je Knoten:** `nodes.<name>.waker` in der Konfigurationsdatei oder der Policy-Schlüssel `waker` über die Admin-API
  (`POST /admin/nodes/<fp>/policy {"waker": "plug"}`, `null` führt zurück zu `wol`). Die Policy nimmt nur `wol` oder einen
  Namen aus `router.wakers` an; Ziele und Verweise auf Geheimnisse stehen nur in der Konfigurationsdatei. Ein unbekannter
  Name in der Konfiguration wird beim Laden abgelehnt.
- **Kein Befehls-Wecker:** Der Router läuft ohne Rechte und führt nichts aus, was die Konfiguration nennt (ADR-0118).
  Einschalten über einen BMC (IPMI, Redfish) oder eine Cloud-API läuft über einen Webhook-Empfänger, der diese Zugangsdaten
  hält; ein eigener `ipmi`-Wecker ist noch nicht umgesetzt.

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

Cloud-Anbieter sind **Modelle im Katalog** (`"openai:gpt-5-mini": {cloud: openai, provider_model: gpt-5-mini, price_per_m: {...}}`)
und liegen als Stufe in genau den Rollen, die es dürfen. Konkrete Cloud-Modellnamen sind nicht direkt aufrufbar (404), nur über
Rollen. Ein kaltes lokales Modell gewinnt vor der Cloud. Drei Fälle, in denen die Cloud zum Zug kommt: kein lokaler Knoten kann
(alle spielen oder aus), eine Fähigkeit fehlt lokal, oder der Client verlangt es (`routing.execution: cloud`).

Schranken (`router.cloud`):

- **Datenklassen als Deklaration**, kein Inhaltsklassifikator: `routing.data_class`, sonst `clients.<c>.data_class`, sonst
  `default_data_class` (**personal**). Cloud nur bis `max_cloud_data_class` (**internal**). Folge: wer nichts deklariert, geht nie
  in die Cloud. Ausnahme: Ein [institutioneller Anbieter](#institutionelle-anbieter) hat seine eigene Grenze.
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
  Muster und Texte für andere Länder und andere Werkzeugformate: [unten](#inhaltsprüfung-länderpakete-und-schwärzer).
  Das Journal hält `privacy_check` mit Ergebnis, Regel oder Wahrscheinlichkeit und Anzahl geschwärzter Einträge fest, nie
  den Text. Eine deklarierte `routing.data_class` wird nie übergangen.
- **Credential-Scan** (`block`): Prompts mit erkennbaren Schlüsseln (`sk-…`, `AKIA…`, `ghp_…`, JWT, `PRIVATE KEY`, `password: …`)
  gehen nicht in die Cloud; lokal laufen sie normal.
- **Budget** je Anbieter und Monat (`budget_month`), in der Währung der Katalogpreise (`price_per_m`, je Million Tokens);
  erschöpft = Anbieter fällt als Stufe weg, ab `warn_at_percent` ein HA-Problem. Kosten je Anfrage in Usage und Metriken.
  Siehe [Währung](#währung-von-preisen-und-budgets).
- **Egress-Allowlist** für `base_url` (+ `egress_allow`), Schlüssel nur aus `secrets.env`, nie im Log; Circuit Breaker wie bei
  Knoten; Schattenläufe gehen nie in die Cloud.
- **Transport:** Anbieter benötigen den endgültigen HTTPS-Endpunkt. HTTP ist nur für explizit erlaubte, wörtliche
  Loopback-Adressen (127.0.0.1 und ::1) möglich. Weiterleitungen des Anbieters führen zu einem Upstream-Fehler.

Adapter: `openai` (auch für OpenAI-kompatible Endpunkte) und `anthropic` (Messages-API). Beide übersetzen in das Ollama-Format,
danach gelten dieselben Wege wie bei Knoten.

**Weitere OpenAI-kompatible Anbieter** (zum Beispiel Mistrals EU-Endpunkt `https://api.eu.mistral.ai/v1`) brauchen keinen Code:
ein Anbieter mit `kind: openai`, seiner `base_url`, `api_key_env` und `secrets_file`, der Host in `router.cloud.egress_allow`
und Katalogeinträge `<anbieter>:<modell>` mit `cloud: <anbieter>` und Preisen. Ihre Reihenfolge in einer Rolle bestimmt, welcher
Anbieter zuerst gefragt wird. Mit `render-env.sh` den Schlüssel in `render-env.conf` als `EXTRA_SECRET_KEYS="EXAMPLE_API_KEY"`
nennen, damit er in `secrets.env` landet (ADR-0080); Namen müssen auf `_KEY` oder `_TOKEN` enden, weil `secrets.env` die Umgebung des Router-Dienstes ist. Siehe den deaktivierten Anbieter `mistral` in
[config.example.yaml](../router/config.example.yaml).

**Denken eines Cloud-Modells abschalten** (Router 0.33.0, [ADR-0098](adr/0098-a-cloud-model-names-the-value-that-turns-its-reasoning-off.md)):
Für einen Katalogeintrag mit `reasoning: true` wird `think: false` eines Clients zu `reasoning_effort` in der Anfrage an
einen `openai`-Anbieter; `think: true` oder kein `think` sendet nichts und lässt die Vorgabe des Anbieters. `reasoning_off`
nennt den Wert: `minimal` (Standard, OpenAIs gpt-5-Modelle) oder `none` (Server, die `minimal` übergehen, zum Beispiel ein
denkendes Modell hinter vLLM). Die Konfigurationsprüfung lehnt andere Werte ab, ebenso den Schlüssel ohne
`reasoning: true` oder an einem lokalen Modell.

```yaml
models:
  "example-llm:reasoner-small":
    cloud: example-llm
    provider_model: example-org/reasoner-small
    reasoning: true
    reasoning_off: none
```

Welchen Wert ein Modell braucht: eine Anfrage mit `think: false` und kleinem `num_predict` (64) schicken. Endet die
Antwort mit `done_reason: length` und leerem Inhalt, oder liegt `eval_count` weit über der Länge der Antwort, denkt das
Modell noch und braucht `none`. Der Gedanke des Anbieters (`reasoning_content`, oder `reasoning`, wie vLLM ihn schickt)
kommt als Ollamas `thinking` an (`reasoning` in der OpenAI-Form, ein Thinking-Block in der Anthropic-Form), außer für
einen Client, der `think: false` gesendet hat.

### Währung von Preisen und Budgets

Router 0.37.0, [ADR-0105](adr/0105-currency-neutral-cost-keys.md): Der Router rechnet nie Währungen um. Preise
(`models.<m>.price_per_m`), Budgets (`router.cloud.providers.<p>.budget_month`), Ausgaben und Usage-Kosten stehen alle in der
Währung, in der du die Preise schreibst. `router.cloud.currency` (Vorgabe `CHF`, drei Großbuchstaben, auch in der Web-UI)
nennt sie für die Anzeige: Die Web-UI zeigt sie bei jedem Betrag, der Anbieter-Schnappschuss trägt `currency`, und
`/metrics` hat `skirnir_cloud_currency_info`. Ändern rechnet nichts um; Preise und Budgets mit ändern.

```yaml
router:
  cloud:
    currency: EUR
    providers:
      openai: { kind: openai, base_url: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY, budget_month: 20 }
models:
  "openai:gpt-5-mini": { cloud: openai, price_per_m: { input: 0.25, output: 2.0 } }
```

- **Alte Namen.** Bis Router 0.36 hießen die Schlüssel `budget_month_chf` und `price_chf_per_m`. Sie gelten weiter mit
  derselben Bedeutung in `config.yaml`, in `roles.yaml` (Katalogeinträge und die Web-UI-Einstellung
  `router.cloud.providers.<p>.budget_month_chf`) und in `PUT /admin/config`. Die Web-UI schreibt die neuen Namen, ein
  gespeicherter alter Name wandert also beim nächsten Speichern mit.
- **Beide Namen an einer Stelle:** Derselbe Wert ist erlaubt; verschiedene Werte werden abgelehnt, die Meldung nennt beide
  Schlüssel. Über Dateien hinweg gibt es keinen Konflikt: Ein Wert in `roles.yaml` übersteuert `config.yaml`, gleich welchen
  Namen jede Datei nutzt.
- **Feld- und Metriknamen** behalten die Endung `_chf` (`spend_month_chf`, `budget_month_chf`, `cost_chf`,
  `skirnir_cloud_*_chf*`), damit Dashboards, Alarme und Home-Assistant-Entitäten weiterlaufen; ihre Werte stehen in der
  eingestellten Währung. Der Home-Assistant-Kostensensor hat weiterhin die Einheit `CHF`.

### Inhaltsprüfung: Länderpakete und Schwärzer

Router 0.42.0 ([ADR-0119](adr/0119-privacy-redaction-per-tool-format-and-locale-pack.md), [ADR-0125](adr/0125-unrecognised-tool-results-pass-unless-the-operator-keeps-them-local.md)).
Ohne die neuen Schlüssel verhält sich die Inhaltsprüfung genau wie bisher (Schweizer und deutsche Muster,
Home-Assistant-Schwärzung, unerkannte Werkzeugergebnisse gehen durch). Alle stehen nur in `config.yaml`; die Web-UI kann
sie nicht ändern.

| Schlüssel unter `router.cloud.privacy_check` | Vorgabe | Bedeutung |
|---|---|---|
| `locale` | `ch-de` | Länderpaket: die Muster und Stichwortgruppen, die die Regeln prüfen, der vorgegebene `refusal` und die Markierung, die ein ganzes Werkzeugergebnis ersetzt. `ch-de`: E-Mail, Schweizer Telefonnummern, IBAN, AHV-Nummer, Straßenadressen mit deutschen Endungen, deutsche und englische Wortanfänge, deutsche Ablehnung. `en`: E-Mail, Telefonnummern (international mit `+`/`00`, nordamerikanisch, national mit führender 0), IBAN, englische Wortanfänge, englische Ablehnung; keine nationale Kennnummer und keine Straßenadresse. Eigene `terms` und `refusal` gelten bei beiden zusätzlich. |
| `redactors` | `[ha_assist]` | Geschwärzte Werkzeugformate, in dieser Reihenfolge. `ha_assist`: Live-Kontext von Home Assistant (Einträge aus `redact_domains`, `redact_device_classes`, `redact_names`). `generic_json`: eingestellte Pfade und Schlüssel von JSON-Werkzeugergebnissen (unten). Mindestens einer; `redact_tools` gilt unabhängig von der Liste. |
| `generic_json` | – | Nötig mit dem Schwärzer `generic_json`, ohne ihn abgelehnt. `tools` ordnet einem Werkzeugnamen, oder `*` für jedes JSON-Ergebnis, Pfade mit Punkten zu, die entfernt werden (`events.*.attendees`: `*` steht für jeden Schlüssel eines Objekts oder jedes Element einer Liste); eine leere Liste kennzeichnet ein Werkzeug als erkannt, ohne etwas zu entfernen. `keys` entfernt Schlüssel dieser Namen in jeder Tiefe dieser Ergebnisse. Ist das Ergebnis eines in `tools` genannten Werkzeugs kein JSON, bleibt die Anfrage lokal. |
| `unrecognised_tool_results` | `pass` | `keep_local`: Ein Werkzeugergebnis, das kein Schwärzer erkennt, hält die Anfrage lokal. Erkannt werden das JSON von Home Assistant mit einem `result`-Text, Text mit Live-Kontext-Einträgen, ein Werkzeug aus `redact_tools` und ein JSON-Ergebnis eines Werkzeugs aus `generic_json.tools`. Bei Home Assistant sind Intent-Ergebnisse wie `HassTurnOn` kein Live-Kontext; solche Werkzeuge mit `[]` unter `generic_json.tools` eintragen, wenn sie durchgehen dürfen. |

```yaml
router:
  cloud:
    privacy_check:
      enabled: true
      locale: en
      redactors: [ha_assist, generic_json]
      generic_json:
        tools: {calendar_events: ["events.*.attendees", owner], HassTurnOn: []}
        keys: [email, phone]
      unrecognised_tool_results: keep_local
```

Eine Anfrage, die die Schwärzung lokal hält, bekommt die Ablehnung wie jede andere; das Journal hält `result: unredacted`
mit `by: redaction:generic_json` oder `redaction:unrecognised` fest. JSON, das tiefer als 512 Ebenen verschachtelt ist
(100 bei `generic_json`), wird nicht geprüft und hält die Anfrage ebenfalls lokal.
`/admin/state` zeigt Länderpaket, Schwärzer, Modus
und für `generic_json` nur die Anzahl Werkzeuge und Schlüssel. Mit `unrecognised_tool_results: pass` (Vorgabe) gehen
Werkzeugergebnisse in Formaten, die kein Schwärzer kennt, unverändert hinaus: Die Inhaltsprüfung schützt, was der Nutzer
geschrieben hat, und den Hauskontext in bekannten Formaten. Schwärzer für openHAB oder ioBroker gibt es noch nicht; sie
kommen zusammen mit Testdaten ihrer echten Formate. Länderpakete kommen nur zusammen mit Testsätzen für ihre Muster. Der
Datenschutz-Klassifikator ist auf deutschen und englischen Sätzen trainiert; ein anderes Länderpaket ändert daran nichts.

### Institutionelle Anbieter

Router 0.34.0, [ADR-0099](adr/0099-institutional-providers-take-their-own-data-class-limit.md):
ein Modellserver, den die eigene Organisation betreibt (Hochschul- oder Firmencluster), nach ihren Datenschutzregeln für
personenbezogene Daten freigegeben. Er wird wie jeder OpenAI-kompatible Anbieter eingetragen, dazu `trust: institutional`
mit eigener `max_data_class`:

```yaml
router:
  cloud:
    egress_allow: [llm.example.org]
    providers:
      institution:
        kind: openai
        trust: institutional          # nur in config.yaml, nicht in der Web-UI änderbar
        max_data_class: personal      # ersetzt max_cloud_data_class für diesen Anbieter
        base_url: https://llm.example.org/v1
        api_key_env: INSTITUTION_API_KEY
        secrets_file: /etc/skirnir-router/secrets.env
        budget_month: 0
```

- Die eigene `max_data_class` **ersetzt** `max_cloud_data_class` für diesen Anbieter. Erlaubt ist jede Klasse unter der
  höchsten, `secret` verlässt das Haus also nie. Mit `trust: cloud` (Vorgabe) schränkt die eigene Klasse eines Anbieters
  die globale Grenze nur ein. Ein höherer Wert wirkt nicht, und der Router warnt beim Start.
- Ohne eigene `max_data_class` gilt für einen institutionellen Anbieter die globale Grenze wie für jeden anderen.
- Alles andere bleibt: `routing.execution: local`, `clients.<c>.cloud: false`, Memory-Projekte mit `allow_cloud: false`
  und verwaltete Sitzungen halten Anfragen von ihm fern. Credential-Scan, HTTPS, Egress-Allowlist, Budget und Circuit
  Breaker gelten.
- Die Inhaltsprüfung (`privacy_check`) läuft nur für Anbieter, deren Grenze unter der Klasse der Anfrage liegt. Ein für
  `personal` freigegebener institutioneller Anbieter bekommt die Anfrage ohne Prüfung und ohne Schwärzung.
- Eine Datenklasse sagt, wie sensibel Daten sind, nicht wem sie gehören. Das institutionelle Modell gehört in eine
  **eigene Rolle**, freigegeben nur für die Clients, deren Daten die Organisation verarbeiten darf (`clients.<c>.roles`).
  Nicht als Ausweichstufe in Rollen hängen, die Haushalts- oder private Clients nutzen.
- `/admin/state` und die Cloud-Tabelle der Web-UI zeigen für jeden Anbieter `trust` und die wirksame Grenze (`ceiling`).
- Den HTTPS-Endpunkt der Organisation verwenden. Eine HTTP-Adresse in ihrem Netz wird abgelehnt wie bei jedem
  Anbieter.
