# Fehlersuche

**Deutsch** · [English](troubleshooting.md) · [README](../README.de.md)

Mit den Logs und der Posture anfangen:

```bash
journalctl -u skirnir-router -f                                         # Router-Log; Posture-Befunde beim Start (Zeilen "posture …")
python3 /opt/skirnir-router/router.py --check /etc/skirnir-router/config.yaml --files
python3 /opt/skirnir-router/router.py --posture /etc/skirnir-router/config.yaml
```

Jede Antwort trägt `X-Skirnir-Request-Id`; dieselbe ID steht im Entscheidungsprotokoll (`/admin/state`, Web-UI *Übersicht*)
und bei Abweisungen im Audit-Log (`client_auth.audit_log`, z. B. `/var/log/skirnir-router/audit.jsonl`).

## Steuer-Port (11435)

| Symptom | Ursache und Abhilfe |
|---|---|
| **503** auf jeder Seite, auch über Loopback | Keine `control_auth.users`. Bei einer Neuinstallation `router.py --init` ausführen, sonst einen Benutzer mit einem Hash aus `router.py --hash` eintragen ([ADR-0053](adr/0053-control-port-requires-users-even-on-loopback.md)). |
| **401** | Falscher Benutzer oder falsches Passwort. Nach wiederholten Fehlversuchen von einer Adresse antwortet der Router eine Minute lang mit **429**. |
| **403** „CSRF protection“ | Eine Nicht-GET-Anfrage an `/admin/*` ohne den Header `X-Skirnir-UI: 1` ([ADR-0042](adr/0042-admin-writes-require-a-custom-header.md)); Skripte müssen ihn senden, die Web-UI tut es. |
| Von einem anderen Rechner nicht erreichbar | `control_listen` ist nicht gesetzt (Standard `127.0.0.1:11435`), oder eine Firewall sperrt 11435. |

## API-Port (11434)

| Status | Ursache und Abhilfe |
|---|---|
| **401** | Unter `client_auth.mode: enforce` hat die Anfrage keine gültige Identität: kein Token, ein falsches Token (Audit `auth_denied`, gezählt als `bad_token`; ein falsches Token fällt nie auf die Quell-IP zurück) oder eine nicht eingetragene Adresse. Den Header prüfen (`Authorization: Bearer <token>`, bei Anthropic-Clients `x-api-key`). |
| **403** | Der Client ist bekannt, darf das aber nicht: Rolle nicht in `roles`, ein konkreter Modellname ohne `models: true`, oder `inference: false` (Audit `forbidden`). |
| **429** | `requests_per_minute` überschritten (Audit `rate_limited`) oder mehr als `max_concurrent` laufende Anfragen (Audit `concurrency_limited`, `Retry-After: 1`). Die Grenze für diesen Client anheben. |
| **413** | Body über dem `max_body_mib` des Clients (Standard 16, Audit `body_too_large`) oder über den globalen Grenzen (`max_images` 16, `max_tools` 128, `max_messages` 1000, Body 64 MiB). |
| **400** mit Klartext | Keine Stufe der Rolle hat eine Fähigkeit aus `routing.require`. |
| **404** | Unbekannte Rolle oder unbekanntes Modell, oder ein konkreter Cloud-Modellname (nur über Rollen aufrufbar). |
| **503** mit Grund-Code | Kein Platz: kein Knoten online, `gpu_busy`, `vram_full`, `memory_pressure` (`Retry-After` 60 s), `server_busy` (`Retry-After` 30 s), Warteschlange voll (`max_queue`) oder Wartebudget überschritten. Anthropic-Clients bekommen stattdessen **529** `overloaded_error`. |
| **424** `rerank_unavailable` | Gerade kann kein Knoten reranken ([reference.de.md](reference.de.md#optionaler-rerank-endpunkt)). |
| **501** auf `/v1/embeddings` | Das Modell ist ein Chat-Modell; Embeddings brauchen ein Embedding-Modell auf dem Knoten. |

Im observe-Modus werden Unbekannte bedient und als `auth_missing` protokolliert; auf `enforce` umstellen, sobald jeder Client
sein Token hat.

## GPU-Knoten und Agenten

| Symptom | Ursache und Abhilfe |
|---|---|
| Agent bleibt *wartend* | Noch nicht freigegeben. Den Fingerprint mit `skirnir-agent identity` auf dem Knoten vergleichen (Administrator/root), dann im Tab *Agenten* freigeben. |
| Agent erscheint gar nicht | Der Tunnel scheitert: `skirnir-agent status`, das Agent-Log (Windows `C:\ProgramData\skirnir-agent\logs\agent.log`, Linux `journalctl -u skirnir-agent`), die Router-Adresse in der Konfiguration und die Firewall für 11435 prüfen. |
| TLS- oder Zertifikatsfehler auf einem Agenten | Der Knoten vertraut dem Zertifikat des Routers nicht. Ein öffentliches Zertifikat nutzen oder `router.ca_file` auf die private CA setzen ([install-agent.de.md](install-agent.de.md#dem-router-zertifikat-vertrauen)); `skirnir-agent check-config` lehnt eine unbrauchbare CA-Datei ab. |
| TLS-Fehler in Home Assistant oder anderen Clients | Dieselbe Ursache: Der Client muss dem Zertifikat auf 11434 vertrauen (öffentliches Zertifikat oder die private CA im Zertifikatsspeicher des Clients). |
| Agent lehnt nach einem Update seine Konfiguration ab | Ein unbekannter Schlüssel auf oberster Ebene; `skirnir-agent check-config` nennt ihn ([ADR-0051](adr/0051-agent-rejects-unknown-top-level-configuration-keys.md)). |
| Update abgelehnt, „ungültige Manifest-Signatur“ | Der Knoten vertraut dem Signierschlüssel nicht; mit `Set-UpdateKey.cmd` oder `update.public_keys` ergänzen ([operations.de.md](operations.de.md#schlüsselwechsel)). Art `downgrade`: Das Release ist älter als der laufende Agent. |
| Knoten zeigt keine Modelle / Rolle nicht bedienbar | Ollama auf `127.0.0.1:11434` nicht erreichbar, oder die Modelle der Rolle sind auf diesem Knoten nicht geladen. |
| Knoten bleibt `busy` | Ein Spiel oder fremde GPU-Last; `skirnir-agent game` zeigt die Spielerkennung, `skirnir-agent gpu` die Sensoren, `/admin/state` `game_heavy` und seinen Grund. |
| Modell langsam | Ein Teil liegt im Arbeitsspeicher: Home Assistant `binary_sensor.skirnir_node_<knoten>_ram_spill` ([operations.de.md](operations.de.md#home-assistant-entitäten)). |

Agent-Befehle zur Diagnose: `skirnir-agent check-config` (Konfiguration), `status` (Dienst und `/health`), `gpu` (Messquelle und
Sensoren), `identity` (Fingerprint), `game` (Spielerkennung und Speicher).

## Gedächtnis-Zustände

Anfragen mit Gedächtnis-Recall tragen `X-Skirnir-Memory-State` ([design/hindsight-memory.md](../design/hindsight-memory.md)):

| Zustand | Bedeutung |
|---|---|
| `available` | Das Gedächtnis hat geantwortet; keine Fakten heißt kein passendes Wissen. |
| `bank_missing` | Unter diesem Scope und Namespace-Schlüssel wurde nie etwas gespeichert (auch nach einem Wechsel des Namespace-Schlüssels). |
| `skipped:tools` | Die Anfrage trägt Werkzeuge, und die Projektfreigabe setzt `recall_into_tools: true` nicht ([ADR-0059](adr/0059-no-automatic-recall-into-requests-with-tools.md)). Weitere `skipped:<grund>`: `no_messages`, `last_not_user`, `content_not_text`. |
| `query_filtered` | Der Filter für sensible Daten hat die Abfrage nicht hinausgelassen; expliziter Recall antwortet 403. |
| `unavailable`, `unavailable:<art>` | Keine brauchbare Antwort (`timeout`, `transport`, `protocol`, `upstream_auth`, `upstream_rejected`). Mit `fail_open: true` läuft die Inferenz ohne Fakten weiter, sonst 503. |

Ein Client, der den Header nicht liest, fällt trotzdem auf, wenn sein Recall wegen Werkzeugen übersprungen wird, etwa
ein Home-Assistant-Eintrag, nachdem dort eine Werkzeug-API eingeschaltet wurde
([ADR-0090](adr/0090-report-recall-skipped-for-tools-per-client.md)): Die Übersicht und `/admin/context`
(`recall_skipped_for_tools`) nennen den Client, sein Projekt, seit wann und wie viele Anfragen, bis eine Stunde nach
seiner letzten solchen Anfrage. `skirnir_memory_recall_skipped_for_tools_clients` zählt diese Clients, und das Log meldet
einmal je Client und Stunde, dass es geschehen ist, ohne Namen. Soll der Client die Fakten bekommen,
`recall_into_tools: true` in seiner Freigabe setzen; damit erreichen Fakten ein Modell, das Werkzeuge aufrufen kann.
Sonst seinen Recall abschalten oder es so lassen.
