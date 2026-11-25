# Routing-Algorithmus – Entwurf v0.1 (2026-09-04)

> Entscheidungen (warum): [ADR-0002](../docs/adr/0002-central-router-in-front-of-gpu-nodes.md), [ADR-0003](../docs/adr/0003-roles-with-ranked-tiers-as-model-names.md), [ADR-0004](../docs/adr/0004-busy-detection-is-a-first-class-scheduler-input.md), [ADR-0006](../docs/adr/0006-go-agent-on-gpu-nodes.md), [ADR-0012](../docs/adr/0012-scheduling-and-admission-in-the-router.md), [ADR-0025](../docs/adr/0025-measured-vram-instead-of-estimates.md)

> **Stand 2026-09-25:** Entwurf vor dem ersten Produktivbetrieb. Umgesetzt und seither erweitert (Score mit Gewichten,
> Breaker-Zustände, Admission, Residenz, Tunnel statt Direktzugriff); die Messvorschrift in Abschnitt 5 ist das Verfahren
> hinter dem Knopf „Messen“ im Modellkatalog. Abschnitt 6 ist entschieden: Go-Agent (seit 0.3.0 mit NVML), der
> PowerShell-Task ist entfernt.

Gehört zur Beispielkonfiguration (`router/config.example.yaml`). Drei Teile: Zustandsermittlung pro Knoten,
Auswahl pro Anfrage, Weiterleitung inkl. WOL. Am Ende: was gemessen werden muss.

## 1. Knotenzustand (läuft dauernd, unabhängig von Anfragen)

Quellen pro Knoten:
- Router pollt selbst `GET /api/tags` (vorhandene Modelle) und `GET /api/ps`
  (geladene Modelle mit `size_vram`) alle `ollama_poll_s`.
- Agent (optional) schickt Heartbeat an `control_listen`:
  `{ts, gpu_util_pct, vram_total_mb, vram_free_mb}`. Sonst nichts. Keine Bewertung im Agent.
- Router kennt seine eigenen laufenden Anfragen pro Knoten (`inflight[node]`).

Ableitungen:
```
ollama_vram      = sum(size_vram über /api/ps)
foreign_vram     = vram_total - vram_free - ollama_vram          # was nicht Ollama gehört
gpu_unknown      = kein Heartbeat seit heartbeat_stale_s
```

Zustandsautomat:
```
offline  <- /api/tags offline_after_misses mal in Folge fehlgeschlagen
free     <- Ollama erreichbar, nicht busy
busy     <- (gpu_util_pct >= busy_enter.gpu_util_pct  seit sustain_s
             UND inflight[node] == 0 während des ganzen Fensters)
            ODER foreign_vram >= or_foreign_vram_gb
            ODER Agent meldet ein Spiel seit busy_enter.game_sustain_s (0.7.0)
busy -> free   erst wenn beide Bedingungen busy_exit.below_for_s lang nicht erfüllt
               (Grund game: busy_exit.game_quiet_s nach dem Spielende, falls laenger)
Speicherdruck  RAM verfuegbar oder Commit frei < memory_pressure.min_free_gib, memory_pressure.polls
               Heartbeats lang: kein Kaltladen, kein Prewarm, nichts entladen (Zustand bleibt free/busy)
```
Warum `inflight == 0`: Ollama-Last, die der Router selbst erzeugt hat, darf den
Knoten nicht als "fremd belegt" erscheinen lassen. Fremde Ollama-Nutzung (z. B. direkt
vom Desktop) fällt damit auch unter "busy", was gewollt ist.

Übergang free -> busy und `unload_on_busy`:
```
für jedes geladene Modell m auf node:
    wenn m in keinem Tier mit busy_ok vorkommt und inflight[node][m] == 0:
        POST node/api/generate {model: m, keep_alive: 0}    # entlädt sofort
```

Messruhe je Knoten (Router 0.21.0, ADR-0061, `router/skirnir_router/quiet.py`): Ein Prüfstand, der Modelle direkt über
Ollama lädt, setzt `POST /admin/nodes/{fp}/quiet {"until": <epoch s>, "reason": "..."}`. Bis `until` gilt für diesen
Knoten:
```
busy_unload_check, Übergang -> busy   entlädt nichts (Logzeile mit Grund, einmal je unload_on_busy_interval_s)
prewarm, residency_check              übersprungen (Logzeile mit Grund)
Belegt-Erkennung                       unverändert: das Messmodell macht den Knoten busy, echter Verkehr bleibt weg
Ende                                   until vorbei (tick_loop) oder DELETE /admin/nodes/{fp}/quiet;
                                       DELETE lehnt mit 409 + would_unload ab, solange busy und das Sicherheitsnetz
                                       dann entlüde, ausser mit {"accept_unload": true}
nach dem Ende                          free -> einmal prewarm (Grund quiet_end); busy -> Sicherheitsnetz wie sonst
```
Der Zustand steht in `/admin/state` (`nodes.<name>.quiet`) und im Journal (`quiet`, `quiet_end`); er lebt nur im
Speicher, ein Router-Neustart beendet ihn.

Entladen mit Freigabe-Nachweis (Router 0.44.0, Agent 0.37.0, ADR-0148/0149, `router/skirnir_router/unload.py`): Der
Agent meldet im Heartbeat `ollama_runners: {count, private_mib}`, die Runner-Prozesse im Prozessbaum seines Ollama-Kinds;
der Router zeigt sie nur bei frischem Heartbeat (`Node.runners_known`). `POST /admin/nodes/{fp}/unload` läuft so:
```
Vorbedingung     offline, OpenAI-Upstream, kein Runner-Fakt, schon ein Entladen, Messung/Benchmark auf dem Knoten
                 -> 409, nichts entladen; Body kein JSON-Objekt -> 400
entladen         /api/ps lesen, je Modell {model, keep_alive: 0}
warten           bis /api/ps leer (t_leer), dann bis ein Heartbeat mit hb_ts > t_leer count 0 meldet -> 200 released
Abbruch          timeout_s (Standard 120, höchstens 600) -> 503 reason timeout (+ loaded); Fakt verschwunden ->
                 not_reported; Knoten offline -> offline; Ollama nicht erreichbar -> 502
```
Ein Heartbeat mit count 0 von vor dem leeren `/api/ps` zählt nicht: Er kann vor dem Start eines Runners entstanden sein.

Knoten ohne GPU (Router/Agent 0.36.0, [ADR-0102](../docs/adr/0102-the-agent-starts-without-a-gpu.md)): Der Agent meldet
`gpu_present: false` in den Fakten und jedem Heartbeat (fehlt das Feld, gilt der Knoten als GPU-Knoten) und schickt keine
GPU-Werte. Der Knoten rechnet im Hauptspeicher:
```
ollama_held   = sum(size über /api/ps)            # statt size_vram, das dort 0 ist; kein ram_spill
reserve       = max(vram_reserve_gib[state], memory_pressure.min_free_gib)
budget        = memory.ram_available_gib + ollama_held - reserve      # ohne frischen Heartbeat: nichts passt
capacity      = memory.ram_total_gib              # fuer model_too_large und "Rolle frei moeglich"
busy          nur ueber ein gemeldetes Spiel; keine Auslastung, kein fremdes VRAM
GPU-Schutz    Zustand not_applicable: kein Problem, keine Deckelung
```
Prewarm nimmt dasselbe Budget. Die VRAM-Messung des Katalogs braucht weiter eine GPU.

## 2. Auswahl pro Anfrage

Eingang: `POST /api/chat | /api/generate | /api/embed`, Body mit `model`, optional
`options.num_ctx`, `keep_alive`, `stream`, `tools`, `format`.

```
def route(request):
    name = request.model
    if name in roles (über exposed_as):
        tiers = roles[name].tiers
    elif expose_concrete_models and name in catalog:     # client needs models: true (ADR-0052)
        tiers = [ {model: name, num_ctx: request.num_ctx or default} ]
    else:
        return 404 {"error": "model 'X' not found"}          # Ollama-Fehlerformat

    client_ctx = request.options.num_ctx  (None = kein Limit vom Client)

    for tier in tiers:                                        # strenge Reihenfolge
        ctx = min(client_ctx, tier.num_ctx) if client_ctx else tier.num_ctx
        need_gb = weights(tier.model) + kv_per_1k(tier.model) * ctx/1000 + 0.8

        candidates = []
        for node in nodes:
            if node.state == offline:                 continue
            if tier.model not in node.models:         continue
            if node.state == busy and not tier.busy_ok: continue
            budget = node.vram_free_gb + node.ollama_vram_gb - vram_reserve_gb[node.state]
                     (alles, was Ollama selbst hält, ist verdrängbar bzw. wird per
                      Runner-Sharing wiederverwendet; fremdes VRAM zählt nicht)
            if gpu_unknown(node): budget = node.vram_total_gb - vram_reserve_gb.free
            if need_gb > budget:                      continue
            candidates.append(node)

        if candidates:
            return dispatch(pick(candidates, tier, role), tier, ctx)

    # kein Tier auf keinem Knoten bedienbar -> Weckversuch
    return try_wake_and_retry(request, tiers)


def pick(candidates, tier, role):
    sortiere nach:
      1. tier.model bereits geladen (True zuerst)
      2. inflight[node]                (weniger zuerst)
      3. vram_free_gb                  (mehr zuerst)
      4. node.weight                   (höher zuerst)
    return erster

    # UPDATE 2026-09-04: "warm zuerst" ist jetzt GLOBAL Standard (modes.warm_first), latency_first pro Rolle nur
    # noch Override. Gegenmaßnahme gegen das Absinken der Liste: prewarm der Rang-1-Modelle bei online/busy->free.
    # latency_first: Kriterium 1 gilt sogar TIER-übergreifend:
    # ist irgendwo ein Modell eines niedrigeren Tiers bereits geladen und der
    # höhere Tier wäre nur per Kaltstart erreichbar, gewinnt das geladene Modell.
    # Für Sprachbefehle zählt die Sekunde mehr als die Modellgröße.
```

Eigenschaften:
- Qualität vor Ort: Tier-Reihenfolge ist strikt (außer bei `latency_first`). Ein kleines
  Modell wird nur gewählt, wenn kein Knoten den größeren Tier tragen kann. Bei `gross`
  gibt es bewusst keinen kleinen Fallback -> 503 statt schlechter Triage.
- Kontext wird nur nach unten angepasst, nie über den Client-Wert erhöht.
- HA schickt pro Turn die volle Historie, daher keine Session-Affinität nötig.
  Kriterium 1 (geladen) sorgt trotzdem für faktische Stabilität.
- Eine 8-GB-Karte trägt rechnerisch nur die 8b/4b-Tiers. Wird also automatisch
  zum "busy-Ausweich- und Assist-Knoten", ohne Sonderregel.

### Node assessment as implemented (router 0.37.1, ADR-0108)

The draft above has grown; this is how the code decides today. One pure function, `scheduler.assess(node, tier, ctx,
now)`, judges whether a local node that offers the tier's model can serve it and returns `(None, {})` or a base code
with the facts for the client. `candidates_for` keeps the nodes without a code, `blockers()`/`unavailable()` turn the
codes into the 503 reason (`code`, `retry_after_s`, `blockers`, `detail`), so selection and explanation cannot differ.

```
assess(node, tier, ctx, now):
    offline                                   -> node_offline
    draining (agent update)                   -> node_draining
    busy and not tier.busy_ok                 -> gpu_busy (busy_reason, game name)
    breaker does not allow (open, or a half-open probe running)
                                              -> node_failing
    release of the held server (standby serves, the server's model is asked for):
        memory pressure                       -> memory_pressure (node RAM figures)
        agent keeps the server held for RAM   -> memory_pressure (agent's figures, agent: true)
        else                                  -> ok, no budget (the server sizes itself to the card)
    switch to the standby (server answers, the standby's store has the model):
        memory pressure                       -> memory_pressure
        else fit against card_budget_gib(node.state)
    loaded with enough context                -> ok, no budget
    memory pressure                           -> memory_pressure
    fit against budget_gib (free VRAM + Ollama's own - reserve; without a GPU, ADR-0102: available RAM + Ollama's
                            own - the larger of reserve and memory_pressure.min_free_gib)

fit(need, budget):
    need <= budget                            -> ok
    need > empty_room_gib(free)               -> model_too_large (waiting does not help)
    foreign VRAM > 0 and need <= budget + foreign
                                              -> gpu_busy (busy_reason foreign_vram)
    else                                      -> vram_full
```

The selection changes no node. Its one former side effect, moving an expired open breaker to `half_open` (one probe)
with a journal entry, is a separate step, `probe_breakers(tiers, now, exclude, req)`, that the request path
(`proxy._Acquire`) and a shadow run call right before they select; it reaches the same targets the selection judges
(tiers the request does not block, targets not excluded, nodes past the state gate). Views (Home Assistant, `/metrics`,
availability, fallback observation) never call it.

Budgets share helpers: `nodes.empty_room_gib(node, state)` is the empty node minus that state's reserve, the card or,
on a node without a GPU, its main memory (`budget_gib` without a fresh report, the residency room, the too-large test);
`Node.card_budget_gib(state)` is the card minus reserve, desktop baseline and foreign VRAM (standby switch with the
node's own reserve); `Node.prewarm_budget_gib(now)` is that with the free reserve on a GPU node, and the request budget
from available RAM on a node without one. `role_possible_when_free` uses only the too-large test, since it asks past busy, draining and the breaker.
`test/selftest_node_assessment.py` asserts over a matrix of node states that selection, explanation, blockers and probe
agree.

## 3. Weiterleitung

```
def dispatch(node, tier, ctx):
    body = request.body
    body.model            = tier.model
    body.options.num_ctx  = ctx
    body.keep_alive       = keep_alive[node.state]     # Client-Wert wird überschrieben
    if request is for a role and not tier.cloud:        # Router 0.30.0, ADR-0093
        for key, value in role.options:                # Sampling-Vorgaben der Rolle, nur was der Client nicht schickt
            body.options.setdefault(key, value)        # OpenAI-Knoten: als Felder, repeat_penalty -> repetition_penalty
    if request is for a role and body.messages and role.system_append[tier.model]:   # Router 0.31.0, ADR-0094
        body.messages = with_system_append(body.messages, role.system_append[tier.model])
        # an die fuehrende Systemnachricht angehaengt (sonst neue); nur das Modell, das bedient; Client-Body bleibt
    inflight[node] += 1
    try:
        antwort = stream POST node.ollama + request.path, body
        # Fehler VOR dem ersten Byte: nächsten Kandidaten desselben Tiers probieren
        # Fehler NACH dem ersten Byte: abbrechen, Fehler durchreichen (kein Doppel-Output)
        # /api/chat und /api/generate: NDJSON Chunk für Chunk durchreichen,
        #   Feld "model" in jedem Chunk zurück auf den Rollennamen setzen (HA sieht "standard:latest")
    finally:
        inflight[node] -= 1
```

Weitere Endpunkte:
```
GET  /api/tags     -> eine Zeile pro Rolle (exposed_as), Größe/Details vom ersten Tier
GET  /api/show     -> Rolle -> /api/show des ersten Tier-Modells auf irgendeinem Online-Knoten
                      (HA liest hier die Capabilities wie "tools"; deshalb müssen alle Tiers einer
                      Rolle dieselben capabilities haben, sonst Konfigurationsfehler beim Start)
GET  /api/ps       -> Vereinigung aller Knoten, Modellnamen auf Rollen gemappt
GET  /api/version  -> Version des Routers
POST /api/pull     -> 403, Modelle werden pro Knoten manuell verwaltet
GET  /admin/state  -> JSON: Knoten, Zustand, Modelle, inflight, letzte Entscheidungen
```

## 4. Wecken

```
def try_wake_and_retry(request, tiers):
    for tier in tiers:
        for node in nodes:
            if node.state != offline or not node.wol:        continue
            if tier.model not in node.last_known_models:      continue   # aus letztem Poll
            if now - node.last_wake_attempt < cooldown_s:     continue
            send_magic_packet(node.mac); node.last_wake_attempt = now
            deadline = now + wait_up_s
            while now < deadline:
                sleep(1); poll(node)
                if node.state != offline: return route(request)    # von vorn, normale Auswahl
                alle retry_interval_s: send_magic_packet(node.mac)
    return 503 {"error": "no node available for model 'X'"}
```
Beim Messen des Weckens zwei Fallen beachten: ein Magic Packet vor dem ersten Poll gilt nicht als Weckerfolg, und ein
Rechner im Hybrid-Standby antwortet auf Pings, ohne dass Ollama läuft. Ein vorhandenes HA-Weckskript wird danach
überflüssig; erst entfernen, wenn der Router-Weckpfad einmal nachweislich durchgelaufen ist.

**Wakers (router 0.41.0, ADR-0117, ADR-0124).** `send_magic_packet` above is now one implementation of a waker,
`wakers.Waker` with `wake(node) -> Sent | Problem`, chosen per node (`nodes.<name>.waker` or the policy key `waker`, default
`wol`). Eligibility is `node.wol and waker.ready(node)` (for `wol`: a MAC, as before). The wait-and-poll loop above stays the
only judge of whether the node came up and is shared by all wakers (`wakers.wake`); a waker re-sends every
`retry_interval_s` only if it declares `repeat` (`wol` always, `http_webhook` only when configured). A waker that cannot send
returns a `Problem` with a fixed `kind`, and the router answers 503 without waiting. `http_webhook` posts
`{"event": "wake", "node", "mac"}` to a configured final URL under the ADR-0048 transport rules; there is no command waker.

## 5. Messung (vor dem ersten Produktivbetrieb)

Die Zahlen im Modellkatalog sind Platzhalter. Messvorschrift pro Modell, pro GPU-Typ einmal:
```
1. ollama stop <model>; Chat mit options.num_ctx=8192  -> ollama ps -> size_vram = A
2. ollama stop <model>; Chat mit options.num_ctx=32768 -> ollama ps -> size_vram = B
   kv_gb_per_1k = (B - A) / 24            # 24k Kontext Differenz
   weights_gb   = A - kv_gb_per_1k * 8
3. Gegenprobe bei 65536 (auf der größten Karte): Vorhersage vs. size_vram, Abweichung < 5 %?
```
Heute misst der Router selbst (`POST /admin/measure`, seit 0.3.5 mit den GPU-Werten des Agenten per nvidia-smi statt
`size_vram`): je Kontext alles entladen, laden, den echten Zuwachs lesen, bei 8k, 32k, jedem Kontext einer Rolle und seit
0.43.1 den angefragten `contexts` ([ADR-0146](../docs/adr/0146-a-measurement-takes-the-contexts-the-caller-names.md)),
damit ein Kandidat ohne vorübergehende Rolle dort gemessen wird, wo er liefe. Ergebnis im Katalog: `vram_real_gib` je Kontext.
Vorbedingung auf allen Knoten identisch: OLLAMA_KV_CACHE_TYPE=q8_0, OLLAMA_FLASH_ATTENTION=1.
Wenn `size_vram` kleiner als `size` ist, läuft Partial-Offload -> Tier passt NICHT, auch
wenn Ollama es lädt. Deshalb Fit-Prüfung im Router, nicht Ollama entscheiden lassen.

## 6. Offene Entscheidungen

- Agent auf Windows: Go-Einzelbinary (NVML) vs. PowerShell-Task mit nvidia-smi.
  Auf gpu-desktop gibt es kein Python. Empfehlung: PowerShell zuerst (kein Build),
  Go wenn sich der Heartbeat als unzuverlässig erweist.
- Schwellen 40 % / 10 s / 2 GB sind Startwerte; nach einer Woche `/admin/state`-Log auswerten.
- WOL für Rechner, die jemand anderem gehören, nur mit Absprache, sonst weckt eine
  nächtliche Automation fremde Rechner.

Cloud tiers send authenticated requests only to the configured final provider URL (ADR-0048). Use HTTPS, or explicitly allowlist literal loopback for local HTTP providers. A redirect is a provider error.
