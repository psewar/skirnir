# Memory: Betriebsfestigkeit und Qualität

[English reference](memory-reliability.md). Ergänzt die
[Shared-Memory-Anleitung](shared-memory-interoperability.de.md).

Neue Defaults bewahren das bisherige Verhalten: `hindsight.outbox_workers: 1`,
`hindsight.outbox_client_capacity: 0`, `context_manager.tool_result_strategy: prefix`.
Alle Verbindungswerte und Schlüssel bleiben extern.

## Kapazität und Wiederaufnahme

Eine positive Client-Kapazität begrenzt aktive Aufträge je authentifiziertem Owner
über alle Projekte. Sie sollte kleiner als die globale `outbox_capacity` sein.
Das schützt freie Plätze vor einem einzelnen Client, garantiert aber keine strikte
Round-Robin-Verarbeitung oder Reservierung für jeden Client. Globale Grenze und
aktuelle Rechte gelten weiterhin. Transaktionale Aufnahme und Retry können die
Grenzen auch bei konkurrierenden Anfragen nicht überschreiten; Replays brauchen
keinen zusätzlichen Platz.

`outbox_workers` erlaubt 1..16 parallele Worker. HTTP bleibt durch `max_inflight`
begrenzt. PostgreSQL-Leases und dieselbe Hindsight-Operations-ID sichern den
Wiederanlauf. Shutdown beendet alle Worker; abgebrochene Aufträge werden nach
Lease-Ablauf wieder übernommen. Das ist keine Exactly-once-Transportzusage.
Alle Router-Instanzen müssen dieselben aktuellen Rechte und Einstellungen nutzen.

`POST /v1/skirnir/memory/operations/{uuid}/retry` erhält zum Beispiel
`{"retry_id":"synthetic-recovery-1"}`. Nur eigene Dead Letters werden unter derselben
UUID und demselben Inhalt erneut eingeplant. Retain verlangt aktuelle Schreibrechte,
aktivierte Retention und weiterhin zulässigen Inhalt; Lösch-Retry verlangt den
separaten Delete-Grant und wartet weiterhin auf tatsächlichen Upstream-Abschluss.
Bank-Bindung und Kapazität werden erneut geprüft. Abgebrochene, entzogene oder
gelöschte Aufträge werden nicht wiederbelebt; bereinigte Retain-Payloads sind nicht
wiederherstellbar. Höchstens drei explizite Recovery-Runden sind möglich, jeweils
mit dem konfigurierten Retry-Limit und Backoff.

Dieselbe Retry-ID für Wiederholungen derselben Aktion verwenden. Alle drei IDs
bleiben im begrenzten Ledger: Auch eine verspätete alte Anfrage eröffnet keine neue
Runde. HTTP 202 bestätigt nur den lokalen dauerhaften Zustand. Die Gesamtzahl der
Versandversuche wird nie zurückgesetzt. Ein früher versendeter Auftrag wird dadurch
nicht fälschlich als unversendet abbrechbar. Neue fachliche Absicht braucht ein neues
Event, keine Löschung bestehender Idempotenzdaten.

Der Status ergänzt `last_error` und `retry_rounds`. Fehler werden ausschliesslich
als feste Kategorien gespeichert: unavailable, timeout, transport, upstream_auth,
upstream_rejected, protocol, remote_pending, policy_changed, lease_expired.
Keine Fehlertexte, Endpunkte, Credentials oder Upstream-Bodies werden persistiert.
Die Kategorie beweist nicht, dass der Upstream noch keine Arbeit ausgeführt hat.

## Datenbank und Überwachung

Migration **4** ergänzt Diagnose-/Retry-Felder, partielle Indizes und
`outbox_totals`. Migrationen 1..3 bleiben unverändert. Bestehende Zustände werden
einmal aggregiert; transaktionale Trigger halten die höchstens acht Zählerzeilen
bei Übergängen, Löschung und Rollback korrekt. Ein kurzer Advisory Lock verhindert
unterschiedliche Sperrreihenfolgen. Direkte SQL-Wartung darf diese Trigger nicht
umgehen oder nur die Job-Tabelle unabhängig von den Zählern leeren.

Statusabfragen gruppieren nicht mehr den gesamten historischen Ledger. Indizes
begrenzen die Suche nach dem ältesten aktiven Auftrag, Owner-Quoten und terminalen
Payloads. `/admin/context` liefert den im Hintergrund alle zehn Sekunden aktualisierten
Cache inklusive `checked_at`; Browser-Refreshs lösen keine zusätzlichen DB-Probes aus.
`skirnir_memory_outbox_oldest_seconds` ist das Alter seit Erstellung des ältesten
aktiven Auftrags, keine Extraktionslatenz oder Dauer seit dem letzten Retry.

Die terminale Payload-TTL verdichtet auch den Scope auf seine unveränderliche
Bank-Bindung. Owner/Projekt, UUID, Fingerprint, Retry-IDs und Zeitpunkte bleiben für
Rechte, Status, gezielte Löschung und Replay-Schutz erhalten. Dieser Ledger wächst
weiter: Automatisches Verfallen der IDs könnte alte Inhalte erneut anlegen.
Speicher- und Backup-Aufbewahrung entsprechend planen. Bereinigte Retain-Payloads
lassen sich nicht wiederholen; eine berechtigte Quellenlöschung bleibt möglich.

Rollback: alte Konfigurationswerte im aktualisierten Build setzen. Ältere Binaries
lehnen Schema 4 ab; Code-Downgrade erfordert einen geprüften Kompatibilitätsbuild oder
einen geplanten Restore. Migrationen und Versionsstand nicht nachträglich ändern.
DB-Restore macht Hindsight-Seiteneffekte nicht rückgängig.

## Provenienz und Kontextqualität

Quellenreferenzen werden einzeln geprüft und als vollständige JSON-Liste erhalten:
maximal 16 Referenzen mit je höchstens 256 Zeichen. Kein Abschneiden mitten im JSON.
Fehlerhafte Listen und nicht unterstützte Objektmetadaten werden ausgelassen.
Ein vollständiger Recall-Eintrag muss weiterhin ins bestehende Wrapper-Budget passen;
Provenienz umgeht keine Kontextgrenze. Inhalte bleiben grundsätzlich untrusted.

Mit aktivem `reduce_tool_results` kann `tool_result_strategy: head_tail` den Anfang
und das Ende alter abgeschlossener Tool-Ergebnisse erhalten, mit expliziten
Auslassungs- und Quellmarkern. `prefix` bleibt Default. Systeme/Developer, offene
Eingaben, aktueller Tail und Checkpoints bleiben exakt, Originalereignisse unverändert.
Wiederholte Compaction unter denselben Einstellungen reduziert denselben Tool-Text
nicht erneut. Ein Strategiewechsel kann früher ausgelassenen Text nicht aus einer
alten Projection rekonstruieren; dafür die Originalereignisse abrufen.

Neue synthetische Qualitätstests prüfen Entscheidungen, abschliessende Fehler,
offene Aufgaben, Tool-Paare und unveränderte Originale auch bei wiederholter
Compaction und kleinerem Modellfenster. Der bisherige Grössenbenchmark bleibt eine
Grössen-/Stabilitätsmessung, keine Garantie semantischen Modellverständnisses.

Der Gateway akzeptiert zusätzlich den aktuellen Hermes-Default
`observation_scopes: shared` und übernimmt ihn in Outbox und Upstream-Auftrag.
Andere Scope-/Extraktionsoptionen bleiben gesperrt. Alte Aufträge ohne diese Option
behalten ihre Fingerprints; eine nachträgliche Optionsänderung desselben Events
liefert einen Konflikt statt einer stillen Überschreibung.

## Echte Integrationstests

Normale PR-CI verwendet synthetische HTTP-Dienste und PostgreSQL 16.
`test/integration_live_memory.py` läuft nur mit `SKIRNIR_RUN_LIVE_MEMORY_TEST=1`;
ohne Opt-in werden echte Tests ausdrücklich als übersprungen ausgewiesen.
Bei Opt-in mit fehlender Konfiguration scheitert der Test.

Der separate manuelle Workflow **Opt-in real Hindsight memory smoke** startet
Wegwerf-PostgreSQL und einen ausschliesslich über Loopback erreichbaren Hindsight-
Container. Er benötigt offizielles Image (für Reproduzierbarkeit Digest festlegen),
Provider/Modell und das Secret `SKIRNIR_LIVE_TEST_LLM_KEY`. Geprüft werden echte
SDK-Annahme, Extraktionsabschluss, gemeinsamer Recall, verweigerter Fremdzugriff und
gezielte Quellenlöschung einschliesslich Dokument-Abwesenheit. Embedding-Modelle
können heruntergeladen werden; synthetische Extraktion kann Providerkosten erzeugen.
Container/Volumes werden anschliessend entfernt, Modell-/Containerlogs nicht publiziert.
Normale PR-CI braucht weder diesen Dienst noch Modellschlüssel.

Alternativ extern `SKIRNIR_TEST_SESSION_DSN`, `SKIRNIR_TEST_HINDSIGHT_URL` und bei
Bedarf `SKIRNIR_TEST_HINDSIGHT_TOKEN` für isolierte Dienste bereitstellen und das
Skript mit Opt-in ausführen. Nur Loopback-Hindsight wird akzeptiert. Keine echte
Session-Datenbank verwenden. Testwissen und Bank-Namespace sind zufällig/synthetisch.

Für den **echten offiziellen Hermes-Provider** im installierten Hermes-Environment
zusätzlich `SKIRNIR_TEST_HERMES_PLUGIN` auf dessen Plugin-Verzeichnis setzen.
Der Test importiert den Provider mit seiner echten Runtime und verwendet seine
Retain-/Recall-Tools über den kurzlebigen Skirnir-Gateway. Nur die Konfiguration
wird ersetzt, keine HTTP-/SDK-Antworten. Vertrauenswürdige feste Plugin-Version nutzen.
Ohne Plugin-Verzeichnis meldet dieser Test einen separaten Skip; die manuelle
Container-CI installiert keinen vollständigen Agenten und führt den SDK-Fall aus.
Ein vollständiger modellgesteuerter Hermes-Agent-Lauf ist damit nicht zugesichert.

Vor Cleanup wird tatsächlicher Abschluss abgewartet. Nur eigene UUID-Dokumente
werden entfernt, niemals die ganze Bank. Cleanup-Fehler machen den Test rot;
den isolierten Dienst vor Wiederverwendung prüfen. Fehlende Marker nach stochastischer
Extraktion sind ein Testfehler, kein Erfolg. Ohne isolierten Backend-/Modellzugang
bleiben echte Extraktions- und Provider-Tests ausdrücklich **nicht durchgeführt**.
