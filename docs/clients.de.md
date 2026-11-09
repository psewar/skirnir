# Clients anbinden

**Deutsch** · [English](clients.md) · [README](../README.de.md)

Clients sprechen den API-Port **11434** an. Jeder Client bekommt eine eigene Identität und ein eigenes Token (Web-UI, Tab
*Clients*, oder `client_auth.clients` in der Konfiguration, siehe
[configuration.de.md](configuration.de.md#client-identitäten-auf-dem-inferenz-port)); das Token wird einmal angezeigt. Clients
fragen nach einer **Rolle** wie `standard:latest`, nicht nach einem Modell.

| Client | Basis-URL | Modell | Zugangsdaten |
|---|---|---|---|
| Ollama-API (Home Assistant, Node-RED, Skripte) | `https://<name>:11434` | Rolle, z. B. `standard:latest` | `Authorization: Bearer <token>` |
| OpenAI-kompatible Werkzeuge und SDKs | `https://<name>:11434/v1` | Rolle | API-Key = Token |
| Anthropic-SDKs, Claude Code | `https://<name>:11434` | Rolle, z. B. `code:latest` | `ANTHROPIC_AUTH_TOKEN` oder `ANTHROPIC_API_KEY` = Token |
| Clients, die keinen Header senden können | `https://<name>:11434` | Rolle | Quell-IP (`ip:` im Client-Eintrag) |

Alle Endpunkte: [reference.de.md](reference.de.md#schnittstellen).

## Home Assistant

1. In der Web-UI im Tab *Clients* einen Client `home-assistant` mit den erlaubten Rollen anlegen; das Token kopieren (wird
   einmal angezeigt).
2. In Home Assistant: *Einstellungen → Geräte & Dienste → Integration hinzufügen → Ollama*.
3. URL `https://<name>:11434/`, API-Key = das Client-Token.
4. Modell = der Rollenname, z. B. `standard:latest` (oder `auto:latest` mit der [Auto-Rolle](configuration.de.md#auto-rolle-die-decision-engine)).

Die Integration schickt den Key als `Authorization: Bearer`. Home Assistant muss dem Zertifikat des Routers vertrauen
(öffentlich oder die private CA in seinem Zertifikatsspeicher). Sprachassistenten profitieren von einer Rolle mit
`priority: interactive` und einer `busy_ok`-Stufe, damit sie weiter antworten, während auf einem Knoten gespielt wird. Home
Assistant kann den Router außerdem als Gerät mit Sensoren über MQTT zeigen
([operations.de.md](operations.de.md#home-assistant-entitäten)).

## Node-RED

- Knoten, bei denen sich Header setzen lassen (z. B. *http request*): POST an `https://<name>:11434/api/chat` mit
  `Authorization: Bearer <token>` und einer Rolle als `model`.
- `node-red-contrib-ollama` schickt seinen Key nur an ollama.com und kann sich daher nicht anmelden: Seinem Client stattdessen
  die Quelladresse des Node-RED-Hosts geben (`ip: ["192.0.2.20"]` im Client-Eintrag). Ein falsches Token fällt nie auf die IP
  zurück.

## OpenAI-kompatible Clients und SDKs

```bash
curl https://<name>:11434/v1/chat/completions -H "Authorization: Bearer <client token>" -H "Content-Type: application/json" \
  -d '{"model":"standard:latest","messages":[{"role":"user","content":"Hello"}]}'
curl https://<name>:11434/v1/models -H "Authorization: Bearer <client token>"
```

SDKs: Basis-URL `https://<name>:11434/v1`, API-Key = das Client-Token, Modell = eine Rolle. Der Router übersetzt `/v1` selbst
nach Ollamas `/api/chat`, damit Rollen, Kontextstufen und `keep_alive` gelten; SSE-Streaming, `tools` und `response_format`
funktionieren. `/v1/embeddings` braucht ein Embedding-Modell auf dem Knoten.

Nativ über Ollama:

```bash
curl https://<name>:11434/api/chat -H "Authorization: Bearer <client token>" \
  -d '{"model":"standard:latest","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

## Anthropic-Clients (Claude Code)

Claude Code und die Anthropic-SDKs sprechen die Messages-API. Zeigen sie auf den Router (`ANTHROPIC_BASE_URL=https://<name>:11434`,
Modell = eine Rolle wie `code:latest`), bekommen sie dasselbe wie alle anderen Clients: die Stufen der Rolle, `num_ctx` der Stufe
(oder den größeren Kontext, mit dem das Modell schon geladen ist), warm zuerst, Warten bei `gpu_busy`, Tool-Call-Rettung,
Client-Identität, Metriken und Entscheidungslog (`via=anthropic`). Ollamas eigenes `/v1/messages` kann das nicht: Es kennt kein
`num_ctx`, das Modell lädt mit Ollamas Default-Kontext neu und verdrängt den Runner, den alle anderen Clients teilen.

| Anthropic | Ollama `/api/chat` |
|---|---|
| `system` (String oder Text-Blöcke, `cache_control` fällt weg) | eine `system`-Nachricht |
| `text`- / `image`-Blöcke (Base64) | `content` / `images`; Bild-URLs → 400 |
| `tool_use` (Assistent) / `tool_result` (Nutzer) | `tool_calls` / je Ergebnis eine `role: tool`-Nachricht, `tool_name` aus der zugehörigen `tool_use`-id; `is_error` → Präfix `Error: ` |
| `thinking` / `redacted_thinking` im Verlauf | fällt weg |
| `role: system` mitten in `messages` (gibt es in der öffentlichen API nicht, Claude Code 2.x schickt es) | eine `system`-Nachricht an derselben Stelle |
| `tools` (`input_schema`) | `tools` als Funktionen; Server-Werkzeuge von Anthropic ohne Schema fallen weg |
| `tool_choice` | `none` lässt die Werkzeuge weg; `any` / `tool` werden zum Systemhinweis (erzwingen kann Ollama keinen Aufruf) |
| `max_tokens`, `temperature`, `top_p`, `top_k`, `stop_sequences` | `num_predict`, `temperature`, `top_p`, `top_k`, `stop` |
| `thinking: {type: enabled\|adaptive}` | `think: true`; fehlend oder `disabled` → `think: false`; `display: omitted` → Denkblöcke ohne Text (mit `ping`-Ereignissen, solange das Modell denkt) |

Antworten tragen `id: msg_<Request-ID>` (wie `X-Skirnir-Request-Id`), die angefragte Rolle als `model`, je einen `thinking`-,
`text`- und pro Aufruf einen `tool_use`-Block und `stop_reason` `tool_use`, `max_tokens` oder `end_turn` (Ollama unterscheidet
Stop-Sequenz und natürliches Ende nicht). Der Stream folgt Anthropics Ereignisfolge: `message_start`, je Block
`content_block_start`/`_delta`/`_stop` (Denken endet mit leerem `signature_delta`, ein Tool-Call ist ein `input_json_delta` mit
den ganzen Argumenten), dann `message_delta` mit den echten Zählern und `message_stop`. Ollama schickt seine Kopfzeilen erst
mit dem ersten Token, bei 196k Kontext bis zu zwei Minuten; nach `router.anthropic.keepalive_s` (15 s) ohne Antwort öffnet der
Router den Stream und schickt `ping`-Ereignisse. Ab dann beendet ein scheiternder Knoten den Stream mit einem `error`-Ereignis,
statt auf den nächsten Kandidaten auszuweichen; schnelle Fehler kommen vorher und behalten den Ausweich.

Fehler in Anthropic-Form `{"type": "error", "error": {"type", "message"}}`. Kein Platz (kein Knoten, `gpu_busy`, `vram_full`,
Wartefrist) ist **HTTP 529 `overloaded_error`** mit `Retry-After`, wo Warten hilft – Claude Code wiederholt 529 von selbst;
Grund-Code und Hindernisse stehen in `error.skirnir`. `count_tokens` liefert eine Schätzung (Zeichen je Token wie die
Kontextprüfung). Claude Code schickt `ANTHROPIC_AUTH_TOKEN` als `Authorization: Bearer` und `ANTHROPIC_API_KEY` als `x-api-key`,
der Router nimmt beides. `anthropic-version` und `anthropic-beta` werden angenommen und nur geloggt.
`router.anthropic.enabled: false` schaltet die Endpunkte ab.

`router.anthropic.think` (auch in den Einstellungen der UI, wirkt ohne Neustart): `auto` folgt dem `thinking`-Block des
Clients, `on` und `off` setzen es fest. Claude Code verlangt bei jedem Zug adaptives Denken; ein Modell, das nur an oder aus
kennt (wie qwen3.8), denkt dann bei jedem Zug, auch beim bloßen Lesen einer Datei. Das Entscheidungslog führt `think` je
Anfrage.

## Der routing-Block

Der Client beschreibt, der Router entscheidet. Jeder Inferenz-Request darf einen `routing`-Block tragen. Ollama sieht ihn nie.

```json
{"model": "standard:latest", "messages": [...],
 "routing": {"require": ["tools"], "prefer": ["thinking"], "min_context": 32768,
             "priority": "interactive", "session_id": "unterhaltung-17",
             "data_class": "internal", "execution": "auto", "request_id": "ha-4711"}}
```

| Feld | Wirkung |
|---|---|
| `require` | Pflicht-Fähigkeiten (`tools`, `vision`, `thinking`, `structured`, `embedding`, `insert`, `completion`). Stufen ohne sie fallen weg; kann keine Stufe der Rolle, kommt **400** mit Klartext. |
| `prefer` | Wunsch: Gibt es Stufen mit diesen Fähigkeiten, kommen nur sie in Frage, sonst wird der Wunsch ignoriert. |
| `min_context` | Untergrenze für den Kontext der Stufe. |
| `priority` | `interactive` < `normal` < `batch` für die Admission; sonst gilt die Rolle, gekappt durch das Client-Maximum. |
| `deadline_ms` | Maximale Wartezeit in der Warteschlange, danach 503 mit Klartext. |
| `session_id` | Affinität: Dieselbe Sitzung bleibt auf ihrem warmen Knoten (`session_affinity_ttl_s`, 30 min). |
| `data_class`, `execution` | Datenklasse (`personal` < `internal` < `public`) und `auto` / `local` / `cloud` für die [Cloud-Stufen](configuration.de.md#cloud-als-stufe-in-rollen). |
| `idempotency_key` | oder Header `Idempotency-Key`: Wiederholungen ohne Stream liefern dieselbe Antwort (`X-Skirnir-Idempotent-Replay: 1`). |
| `request_id` | Wird in Antwort, Header und Protokoll gespiegelt; sonst `X-Request-Id` oder vergeben (`skirnir-…`). |

Auch ohne Block liest der Router den Request: `tools` → tools, Bilder → vision, `think: true` → thinking, `format` → structured,
`suffix` → insert. Fähigkeiten stammen aus Ollamas `/api/show`, ergänzt um Katalog-Overrides (`models.<name>.capabilities:
{structured: false}` für Modelle, die ein Schema mit HTTP 500 quittieren). Unbekannte Fähigkeiten blockieren nie.

Die Antwort trägt immer die Header `X-Skirnir-Request-Id`, `-Node`, `-Model`, `-Tier`, `-Warm`; eine Anfrage mit
Gedächtnis-Recall zusätzlich `X-Skirnir-Memory-State` (`available`, `bank_missing`, `query_filtered` oder `unavailable[:<art>]`,
siehe [design/hindsight-memory.md](../design/hindsight-memory.md)); eine Anfrage, deren Antwort gespeichert werden kann,
trägt `X-Skirnir-Memory-Operation` mit der Operations-ID, um sie später zu löschen (ADR-0076). Der Block `routing` im Body (Knoten, Modell, Stufe,
`reason`, `skipped` mit Grund je übersprungener Stufe, `candidates` mit Score, `queued_ms`, `decision`) kommt nur, wenn der
Client selbst einen `routing`-Block geschickt hat – Altclients sehen einen unveränderten Body. Bei `/v1` steht er im
`chat.completion` bzw. im letzten SSE-Chunk.

Größenlimits vor dem Backend (413): `max_images` 16, `max_tools` 128, `max_messages` 1000, Body 64 MiB. Das eigene
`max_body_mib` eines Clients (Standard 16) greift schon davor.

## Gedächtnis und Sitzungen

Persistente Sitzungen, Langzeitgedächtnis (`routing.memory`), verwalteter Kontext und gemeinsames Gedächtnis sind optional und
standardmäßig aus. Überblick und Links zu den Einrichtungsanleitungen:
[reference.de.md](reference.de.md#optionaler-persistenter-session-speicher).
