# Protokoll zwischen Router und Agent

Wie ein Knoten-Agent mit dem Router spricht: was man umsetzen muss, um einen eigenen Agenten zu schreiben (etwa für eine
Plattform, die der Go-Agent nicht unterstützt), und was Router und Go-Agent einander zusichern. Dies ist **Version 1** des
Protokolls, so wie Router 0.37 und Agent 0.36 es sprechen. Warum es so festgehalten wird:
[ADR-0107](adr/0107-versioned-router-agent-protocol-with-a-written-specification.md) (englisch).

Der maschinenlesbare Teil liegt in [`docs/protocol/`](protocol/): je Nachricht ein JSON-Schema und
[`index.json`](protocol/index.json) mit den Rahmen-Codes, der Durchreich-Liste des Agenten und den Grenzwerten. Wo diese
Seite und ein Schema sich widersprechen, gilt der Code, und die Konformitätstests sind kaputt; bitte melden.

## Überblick

```
Agent                                    Router (Steuerport, /v1/tunnel)
  | -- WebSocket-Upgrade (TLS) --------------> |
  | <------------- Text  {"t":"challenge"} --- |
  | --- Text {"t":"hello", signiert} --------> |   Identität = SHA-256 des Ed25519-Schlüssels
  | <------------- Text  {"t":"status"} ------ |   pending | approved (+ Konfiguration) | revoked
  | --- binär HB (alle 3 s) -----------------> |
  | <--------------------------- binär HBACK - |
  | --- binär OLLAMA (bei Änderung, >= 30 s) > |
  | <----------------- binär REQ (Stream n) -- |   ein Ollama- oder OpenAI-Aufruf eines Clients
  | --- binär RESP, DATA..., END (Stream n) -> |
  | <------------- Text-Steuernachrichten ---- |   config, update, ollama-update, ollama-swap, upstream-*
```

Der Agent baut die Verbindung auf, nie der Router: Ein Knoten braucht keine eingehende Firewall-Regel, kein Zertifikat und
keine feste Adresse ([ADR-0008](adr/0008-outbound-tunnel-from-the-agent.md)). Eine Verbindung trägt Anmeldung, die Fakten
des Knotens, Steuernachrichten und alle Inferenz-Anfragen als gemultiplexte Streams.

## Transport

- WebSocket auf `<Steuer-URL des Routers>/v1/tunnel` (aus `https://` wird `wss://`). Der Router bedient ihn auf seinem
  Steuerport; der Agent prüft das Router-Zertifikat (System-Zertifikatsspeicher oder Agent-Einstellung `router.ca_file`,
  ADR-0070).
- Text-Rahmen tragen JSON-Objekte mit einem Typfeld `t`: Anmeldung und Steuernachrichten.
- Binär-Rahmen tragen `Typ (1 Byte) | Stream-ID (4 Byte, Big Endian) | Nutzlast`.
- Der Router pingt alle 20 s; der Agent antwortet mit Pong und hält 90 s ohne jeden Rahmen für eine tote Verbindung.
- Größen: Das Hello hat höchstens 32 KiB; der Router nimmt Rahmen bis 2 MiB an (der Go-Agent schickt Antwortkörper in
  Stücken von höchstens 32 KiB); der Go-Agent nimmt Rahmen bis 64 MiB an (ein Anfragekörper kommt in einem REQ-Rahmen).
- Neuverbindung: Der Go-Agent versucht es nach 1, 2, 5, 10, dann alle 30 s, nach `revoked` alle 5 Minuten.

## Anmeldung

1. Der Router schickt `{"t":"challenge","nonce":<Base64 von 32 Zufallsbytes>,"router":<Version>}`
   ([challenge](protocol/challenge.schema.json)). Der Agent wartet höchstens 15 s und lehnt eine Nonce unter 16 Byte ab.
2. Der Agent antwortet binnen 15 s mit dem Hello ([hello](protocol/hello.schema.json)):
   `{"t":"hello","node":<Name>,"pubkey":<Base64>,"sig":<Base64>,"facts":{...},"agent":<Version>}`.
   `sig` ist die Ed25519-Signatur über die rohen Bytes `nonce || UTF-8(node) || pubkey`. Der Router kürzt `node` vor der
   Prüfung auf 64 Zeichen, ein längerer Name besteht die Prüfung also nie. Der Agent behält seinen Schlüssel über
   Neustarts; ein neuer Schlüssel ist ein neuer Knoten, den der Betreiber freigeben muss
   ([ADR-0009](adr/0009-agent-identity-by-key-and-operator-approval.md)).
3. Der Router antwortet mit `{"t":"status","state":"pending"|"approved"|"revoked",...}`
   ([status](protocol/status.schema.json)). Mit `approved` kommt das [Konfigurationspaket](protocol/config-bundle.schema.json)
   (Heartbeat-Intervall, Knotenname, MQTT-Zugang, falls eingerichtet).

Ablehnungen: Ein fehlerhaftes Hello oder eine falsche Signatur schließt die Verbindung mit Code 4001, ohne Status. Eine
neue Identität jenseits der Registrierungsgrenzen (32 wartende Identitäten, 6 neue je Adresse in 5 Minuten, ADR-0041)
bekommt `status pending` mit einer Meldung und Close-Code 1008. Eine gesperrte Identität bekommt `status revoked` und
wird getrennt.

Solange `pending` gilt, bleibt die Verbindung offen: Der Router beantwortet Heartbeats mit `state: pending` und verwirft
OLLAMA- und POWER-Rahmen. Bei der Freigabe schickt er auf derselben Verbindung `status approved` mit dem Paket.

## Fakten

Das Objekt `facts` im Hello beschreibt die Maschine ([facts](protocol/facts.schema.json)): Hostname, Betriebssystem und
Architektur, GPU und VRAM, die MAC-Adresse für Wake-on-LAN, Versionen und Schalter für optionale Funktionen. Der Router
speichert es und baut daraus den Knoten. Kein Fakt ist Pflicht, aber ein Agent sollte `hostname`, `os`, `arch` und
`agent_version` schicken; ohne die letzten drei kann der Router keine Agent-Updates anbieten. Funktionsschalter
(`ollama_managed`, `ollama_swap_gate`, `upstream_release`, `upstream_fallback`) sind ausdrücklich: Der Router schickt
die zugehörigen Steuernachrichten nur, wenn der Schalter `true` ist. `gpu_present: false` (Agent 0.36.0) meldet einen
Knoten ohne GPU: Der Router bemisst seine Modelle am Hauptspeicher statt am VRAM (ADR-0102); fehlt `gpu_present`, gilt
eine GPU als vorhanden, wie bei jedem älteren Agenten.

## Rahmen

| Code | Name | Richtung | Stream | Nutzlast |
|---|---|---|---|---|
| 1 | REQ | Router → Agent | n | [Anfragekopf](protocol/request-head.schema.json) als JSON, Zeilenumbruch, Körper |
| 2 | RESP | Agent → Router | n | [Antwortkopf](protocol/response-head.schema.json) `{status, headers}` |
| 3 | DATA | Agent → Router | n | Bytes des Antwortkörpers, ein Rahmen je Stück |
| 4 | END | Agent → Router | n | leer: Antwort vollständig |
| 5 | ERR | Agent → Router | n | UTF-8-Text: der Upstream ist gescheitert |
| 6 | CANCEL | Router → Agent | n | leer: Anfrage abbrechen |
| 7 | HB | Agent → Router | 0 | [Heartbeat](protocol/heartbeat.schema.json) |
| 8 | HBACK | Router → Agent | 0 | [Heartbeat-Antwort](protocol/heartbeat-ack.schema.json) |
| 9 | OLLAMA | Agent → Router | 0 | [Upstream-Zustand](protocol/ollama-state.schema.json) (Agent 0.14.0) |
| 10 | POWER | Agent → Router | 0 | [Energiemeldung](protocol/power.schema.json) (Agent 0.24.0) |

Beide Seiten ignorieren Rahmen unter fünf Byte und Stream-Rahmen für einen Stream, den sie nicht kennen. Der Go-Agent
ignoriert unbekannte Rahmentypen; der Router wertet einen unbekannten Rahmentyp auf einem offenen Stream als Fehlschlag
dieses Streams.

### Anfrage-Streams

Der Router nummeriert Streams ab 1 aufwärts (nach dem Überlauf wieder ab 1, nie 0) und schickt die ganze Anfrage in einem
REQ-Rahmen. Der Agent antwortet mit genau einem RESP, dann beliebig vielen DATA-Rahmen, dann END; oder jederzeit mit ERR,
was die Anfrage scheitern lässt. Ist der erste Rahmen eines Streams kein lesbares RESP, lässt der Router die Anfrage
scheitern. Geht der Client weg oder läuft die Frist ab, schickt der Router CANCEL und vergisst den Stream; der Agent bricht
die Upstream-Anfrage ab und meldet den Abbruch nicht als ERR. Streams laufen parallel.

Was er weiterreicht, entscheidet der Agent selbst, damit ein kompromittierter Router den Upstream des Knotens für nichts
anderes nutzen kann (ADR-0073):

- Ein als Tunnel-Route eingerichteter Pfad (Agent-Einstellung `tunnel.routes`, exakter Pfad ohne Query) geht an seinen
  lokalen Dienst, nur mit den gelisteten Methoden; eine andere Methode bekommt `405` mit `Allow`.
- Jede andere Anfrage geht nur an den Upstream, wenn Methode und Pfad (exakt, ohne Query, ohne Groß-/Kleinschreibung
  anzugleichen) auf der Durchreich-Liste in [`index.json`](protocol/index.json) stehen: Inferenz, Embeddings,
  Modellliste und Versionen. Alles andere, vor allem die Modellverwaltung, bekommt `403`.

Eigene Ablehnungen des Agenten sind ein RESP mit `403` oder `405`, ein DATA-Rahmen mit
[`{"error":{"reason":...}}`](protocol/node-refusal.schema.json) und END.

### Heartbeat

Der Agent schickt HB alle `heartbeat_interval_s` (3 s, bis das Paket etwas anderes sagt); der Router beantwortet jeden
mit HBACK (`state` free, busy oder offline, `busy_reason`, `gpu_guard_ack`). Der Heartbeat trägt `gpu_present` und auf
einem Knoten mit GPU die GPU-Auslastung und den VRAM; als optionale Blöcke Sensoren, den GPU-Schutz, den Stand von
Updates, den VRAM der Kinder des Agenten, ein laufendes Spiel, den Hauptspeicher, die Gesundheit der Tunnel-Routen und
die Modelle des Bereitschafts-Ollama. Der Spiel-Block meldet `enabled: false`, wenn die Spiel-Erkennung auf dem Knoten
abgeschaltet ist (Agent 0.36.0, ADR-0103); `via` ist `path` oder `name` (ältere Agenten schickten das deutsche `pfad`,
das der Router weiter annimmt). Fehlt ein Block, meldet der Agent diesen Fakt nicht; der Router behält dafür sein
bisheriges Verhalten. Einen Heartbeat, den der Router nicht anwenden kann, beantwortet er mit `state: ""` und `error`.

### Upstream-Zustand

Mit OLLAMA meldet der Agent, was sein Upstream hält: `up`, die Einträge von `/api/tags` und `/api/ps` unverändert, und bei
einem OpenAI-kompatiblen Upstream, ob er angehalten, im Start, blockiert oder durch das Bereitschafts-Ollama ersetzt ist.
Er schickt den Rahmen zu Beginn jeder Sitzung, bei jeder Änderung, nach der Freigabe und mindestens alle 30 s. Solange
eine Meldung jünger als 45 s ist, fragt der Router den Knoten nicht selbst ab. `up: false` nimmt den Knoten sofort aus
dem Routing.

### Energie

POWER meldet `suspending` (der Router nimmt den Knoten sofort aus dem Routing) oder `resumed`. Der Agent wiederholt seine
letzte Meldung zu Beginn jeder Sitzung, vor dem ersten Heartbeat.

## Steuernachrichten

Nach der Anmeldung kann der Router diese Text-Nachrichten jederzeit schicken; unbekannte Typen ignoriert der Agent.

| `t` | Schema | Wann |
|---|---|---|
| `status` | [status](protocol/status.schema.json) | Freigabe oder Sperre einer verbundenen Identität |
| `config` | [status](protocol/status.schema.json) | das Konfigurationspaket des Knotens hat sich geändert |
| `update` | [control-update](protocol/control-update.schema.json) | Agent-Update; der Agent vertraut nur der Signatur des Betreibers über das Manifest ([ADR-0020](adr/0020-signed-agent-updates-through-the-router.md)) |
| `ollama-update` | [control-ollama-update](protocol/control-ollama-update.schema.json) | Ollama-Update für Knoten mit `ollama_managed`; der Router wählt die Version, der Agent die Quelle ([ADR-0021](adr/0021-ollama-as-agent-child-updated-through-the-router.md)) |
| `ollama-swap` | [control-ollama-swap](protocol/control-ollama-swap.schema.json) | Freigabe eines geladenen Ollama-Updates auf einem leeren Knoten (`ollama_swap_gate`) |
| `upstream-hold`, `upstream-release` | [control-upstream](protocol/control-upstream.schema.json) | OpenAI-kompatiblen Upstream anhalten oder starten (`upstream_release`) |

## Verträglichkeitsregeln

- **Unbekannte Felder werden ignoriert**, auf beiden Seiten; neue optionale Felder sind der bisherige Weg, wie das
  Protokoll wächst. Jedes Schemafeld nennt, seit welcher Agent-Version es existiert (`x-since-agent`; `baseline` heißt:
  in jedem Agenten dieses Repositorys).
- **Optionale Funktionen meldet der Agent an**, als Fakten; der Router nutzt sie nur, wenn sie angemeldet sind.
- **Required** in einem Schema heißt: Ohne das Feld lehnt der Empfänger die Nachricht ab oder verwirft sie. Alles andere
  ist optional und hat eine beschriebene Bedeutung, wenn es fehlt.
- Felder mit `x-legacy` liest der Router, der aktuelle Agent schickt sie aber nicht; `x-ignored-by-receiver` markiert
  informative Felder; `x-open` markiert Objekte, die anderswo definiert sind (Ollamas Modelleinträge) und die `--strict`
  nicht Feld für Feld prüft.
- Freitextfelder (`message`, `error`, ERR-Nutzlast) sind nur für Logs und Anzeige. Entscheidungen fallen über die festen
  Felder und Codes, nie über den Text ([ADR-0023](adr/0023-explicit-control-flow-and-error-codes.md)).

Eine Protokoll-Versionsnummer und eine Fähigkeitenliste gehören nicht zu Version 1; ADR-0107 beschreibt sie als nächsten
Schritt.

## Sicherheit

- Der Router authentisiert den Agenten über seinen Ed25519-Schlüssel und die Freigabe durch den Betreiber; der Agent den
  Router über TLS. Alles nach der Anmeldung gilt nur so weit, wie die Identität freigegeben ist.
- Der Router behandelt jede Agent-Nachricht als nicht vertrauenswürdige Eingabe: Größengrenzen, Typprüfungen, und ein
  fehlerhafter Heartbeat oder Bericht wird beantwortet oder verworfen, ohne den Tunnel zu beenden.
- Der Agent behandelt den Router bei dem, was er ausführt, als nicht vertrauenswürdig: Durchreich-Liste, signierte
  Agent-Updates, Ollama-Archive aus der eigenen Quelle des Agenten, abgelehnte Downgrades.
- Konfigurationspaket und Update-Token sind Geheimnisse: Agenten dürfen sie nicht loggen.

## Einen Agenten prüfen

`python test/protocol_schema.py <Schema> <Nachricht.json> [--strict]` prüft eine mitgeschnittene Nachricht gegen ein
Schema (`--strict` lehnt auch undokumentierte Felder ab). Der Prüfer braucht außer Python keine Pakete und kennt nur die
JSON-Schema-Schlüsselwörter, die diese Dateien benutzen.

Die Konformitätstests halten Spezifikation und Umsetzungen gleich:

- `test/selftest_protocol.py` prüft die Schemas und ihre Beispiele, vergleicht Rahmen-Codes und Grenzwerte mit dem Router
  und `test/fake_agent.py`, prüft, dass jede Steuernachricht, die der Router baut, und jedes Feld, das er liest,
  dokumentiert ist, dass „required“ im Router gilt, und lässt den echten Router mit dem Test-Agenten über ein Relais
  sprechen, das jede Nachricht in beide Richtungen prüft.
- `agent/protocol_conformance_test.go` vergleicht die Nachrichtentypen und Heartbeat-Schlüssel des Go-Agenten in beide
  Richtungen mit den Schemas, dekodiert jede dokumentierte Router-Nachricht in die Typen des Agenten, vergleicht
  Rahmen-Codes und Durchreich-Liste mit `index.json` und spielt die echte Anmeldung gegen einen Test-Router durch.

Eine Änderung am Protokoll ändert `docs/protocol/`, diese Seite und ihren englischen Zwilling im selben Pull Request; die
Tests schlagen fehl, bis alle drei Seiten übereinstimmen.
