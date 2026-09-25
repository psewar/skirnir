# Routing-Algorithmus – Entwurf v0.1 (2026-09-04)

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
busy -> free   erst wenn beide Bedingungen busy_exit.below_for_s lang nicht erfüllt
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

## 2. Auswahl pro Anfrage

Eingang: `POST /api/chat | /api/generate | /api/embed`, Body mit `model`, optional
`options.num_ctx`, `keep_alive`, `stream`, `tools`, `format`.

```
def route(request):
    name = request.model
    if name in roles (über exposed_as):
        tiers = roles[name].tiers
    elif expose_concrete_models and name in catalog:
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
    # noch Override. Gegenmassnahme gegen das Absinken der Liste: prewarm der Rang-1-Modelle bei online/busy->free.
    # latency_first: Kriterium 1 gilt sogar TIER-übergreifend:
    # ist irgendwo ein Modell eines niedrigeren Tiers bereits geladen und der
    # höhere Tier wäre nur per Kaltstart erreichbar, gewinnt das geladene Modell.
    # Für Sprachbefehle zählt die Sekunde mehr als die Modellgrösse.
```

Eigenschaften:
- Qualität vor Ort: Tier-Reihenfolge ist strikt (ausser bei `latency_first`). Ein kleines
  Modell wird nur gewählt, wenn kein Knoten den grösseren Tier tragen kann. Bei `gross`
  gibt es bewusst keinen kleinen Fallback -> 503 statt schlechter Triage.
- Kontext wird nur nach unten angepasst, nie über den Client-Wert erhöht.
- HA schickt pro Turn die volle Historie, daher keine Session-Affinität nötig.
  Kriterium 1 (geladen) sorgt trotzdem für faktische Stabilität.
- Eine 8-GB-Karte trägt rechnerisch nur die 8b/4b-Tiers. Wird also automatisch
  zum "busy-Ausweich- und Assist-Knoten", ohne Sonderregel.

## 3. Weiterleitung

```
def dispatch(node, tier, ctx):
    body = request.body
    body.model            = tier.model
    body.options.num_ctx  = ctx
    body.keep_alive       = keep_alive[node.state]     # Client-Wert wird überschrieben
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
GET  /api/tags     -> eine Zeile pro Rolle (exposed_as), Grösse/Details vom ersten Tier
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

## 5. Messung (vor dem ersten Produktivbetrieb)

Die Zahlen im Modellkatalog sind Platzhalter. Messvorschrift pro Modell, pro GPU-Typ einmal:
```
1. ollama stop <model>; Chat mit options.num_ctx=8192  -> ollama ps -> size_vram = A
2. ollama stop <model>; Chat mit options.num_ctx=32768 -> ollama ps -> size_vram = B
   kv_gb_per_1k = (B - A) / 24            # 24k Kontext Differenz
   weights_gb   = A - kv_gb_per_1k * 8
3. Gegenprobe bei 65536 (auf der 5090): Vorhersage vs. size_vram, Abweichung < 5 %?
```
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
