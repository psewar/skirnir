# Gemeinsames Gedächtnis, Agent-Übergaben und Zuverlässigkeit

[Englische Referenz mit vollständigem Konfigurationsbeispiel](shared-memory-interoperability.md).
Die Erweiterung nutzt den bestehenden Session Store, den Hindsight-Adapter und die
extraktive Context Engine. Ein neues Agent-Framework oder zusätzliche Modellaufrufe
werden nicht eingeführt.

## Aktivierung und Berechtigungen

Alle neuen Schalter sind standardmässig aus. Bestehende private Bank-IDs behalten
exakt ihre bisherige HMAC-Ableitung aus Principal, Projekt und Datenklasse.
Bestehende Memory-Konfiguration nutzt weiterhin `strategy: legacy` und explizites
`routing.memory`. Normale Inferenz benötigt weder PostgreSQL noch Hindsight.

Für persistente Funktionen `router/requirements-session.txt` installieren. DSN,
Hindsight-HTTPS-Endpunkt, Backend-Token und unabhängige zufällige Namespace- und
Handoff-Signaturschlüssel ausschliesslich extern setzen. Die YAML enthält nur
Umgebungsvariablennamen. Der Namespace-Schlüssel muss stabil bleiben; seine Rotation
ändert Bank-Identitäten. Alte Outbox-Aufträge werden dabei verworfen (`revoked`),
statt in eine neue Bank umgeleitet zu werden. Rotation des Handoff-Schlüssels macht
bestehende Übergabepakete ungültig.

Gemeinsame Scopes werden unter `router.memory_scopes` definiert, beispielsweise
`shared-research: {data_class: personal, allow_cloud: false}`. Ein Client erhält in
`memory.projects.example-project` einen serverseitigen Verweis
`scope: shared-research`, dieselbe Datenklasse und getrennte boolesche Freigaben
`read`, `write`, `delete`, `allow_cloud`. Ohne `scope` bleibt die Bank privat.
Mehrere Clients beziehungsweise Projekt-Aliase können denselben Scope verwenden;
ein Client kann mehrere Grants besitzen. Es gibt keine freie physische Bank-Auswahl.

Cloud-Verarbeitung erfordert beim Shared Scope sowohl die globale als auch die
clientseitige Freigabe und sämtliche bestehenden Routing-Prüfungen. Authentifizierung
über Bearer, Basic oder API-Key ist erforderlich; reine IP-Zuordnung reicht nicht.
Berechtigungen werden pro Anfrage und direkt vor jedem HTTP-Versuch innerhalb des
Concurrency-Slots erneut geprüft. Entzug stoppt wartende Aufträge, kann aber bereits
versendete Daten nicht zurückholen. Massgeblich ist die aktuell geladene Konfiguration
nach einem Reload. Pool-/Backend- und Lifecycle-Änderungen erfordern einen Neustart.
Mehrere Worker müssen dieselben aktuellen Grants und Secrets verwenden.

| Strategie | Verhalten |
| --- | --- |
| `legacy` | Bisheriger Default: explizites `routing.memory`, Router als Memory-Verantwortlicher. |
| `off` | Kein Memory-Zugriff, auch nicht über Tools oder Gateway. |
| `manual` | Nur explizite REST-/Gateway-Zugriffe; auch mit `owner: client`. |
| `auto` | Automatischer Recall; Output-Retention nur mit zusätzlichem `auto_retain: true` und Schreibfreigabe. |
| `hybrid` | Automatischer Recall und explizite Tools, keine automatische Output-Retention. |

Für `auto` und `hybrid` sind `owner: skirnir` und ein freigegebenes `default_project`
erforderlich. Bei gleichzeitiger Session-/Memory-Nutzung müssen die Projekte
übereinstimmen. Neue scoped Strategien umgehen den gemeinsamen Antwortcache und
Shadow-/Observation-Inferenz. Inhalte werden nicht als gewöhnliche Fehlerlogs
weitergereicht. Bestehende unbeteiligte Inferenzpfade bleiben unverändert.

## Hermes sicher anbinden

Das offizielle Hindsight-Plugin gemäss
[Integrationsanleitung](https://hindsight.vectorize.io/sdks/integrations/hermes)
installieren (`hermes plugins install hindsight`) und `memory.provider: hindsight`
auswählen. Im separaten `hindsight/config.json`:

```json
{
  "mode": "local_external",
  "api_url": "https://router.example.net/v1/skirnir/hindsight",
  "bank_id": "example-project",
  "recall_prefetch_method": "recall",
  "recall_types": ["observation"],
  "auto_recall": true,
  "auto_retain": false,
  "memory_mode": "hybrid"
}
```

Den Skirnir-Client-Token über den externen Secret-Mechanismus des Plugins
(`HINDSIGHT_API_KEY`) bereitstellen, niemals den administrativen Backend-Token.
Für diesen Client in Skirnir `memory.owner: client`, `memory.strategy: manual`
setzen; dadurch injiziert und speichert Skirnir dieselben Inhalte nicht zusätzlich.
Die Inferenz-URL bleibt der normale Skirnir-Endpunkt. Zusätzliche Banks müssen
ebenfalls freigegebene Projekt-Aliase sein.

Der Reverse Proxy muss das Gateway-Präfix zu Skirnir routen, HTTPS und Auth-Header
erhalten und darf keinen unkontrollierten direkten Hindsight-Zugang veröffentlichen.
Hindsight per Firewall/Service-Authentifizierung nur für den Router freigeben.
Wer unabhängig uneingeschränkte Backend-Zugangsdaten besitzt, kann Skirnir-Grants
umgehen; das lässt sich nicht im Router verhindern.

Der Adapter unterstützt unter `/v1/skirnir/hindsight` die nativen Recall-/Retain-
und Operationsstatus-Pfade sowie eine versionslose Capability-Antwort. Native
Retain-Antworten verwenden HTTP **200** mit `durable: true` und UUID, weil der
offizielle SDK nur dafür `RetainResponse` deserialisiert. Mit `async: true` bedeutet
das lokale Annahme, keine abgeschlossene Extraktion. Mit `async: false` (Hindsight-
Standard, so ruft das Hermes-Werkzeug `hindsight_retain` in Plugin 1.0.0 auf) wird der
Auftrag genauso dauerhaft angenommen, die Antwort wartet aber auf die **geprüfte
Fertigstellung**, höchstens `sync_wait_s` (20 s). Danach kommt **504** mit der
Operations-UUID: Der Auftrag läuft weiter, identisches erneutes Senden liefert dieselbe
Operation. Höchstens `sync_max_waiting` synchrone Aufträge warten gleichzeitig, darüber
503 ohne Annahme. Skirnirs eigene Retain-API nutzt 202.

Bewusst begrenzt: ein kurzer Texteingang pro Retain, keine freie Tenant-/Bank-
Administration, kein Reflect, Append, Entity-/Chunk-Expansion, Trace oder nicht
unterstützte Filter/Extraktionsstrategien. Solche Optionen werden abgelehnt.
Recall bleibt serverseitig begrenzt und nutzt upstream das niedrige Suchbudget.
Plugin-Metadaten können keine Bestätigung von Assistentenaussagen erzwingen.

Identische Dokument-/Inhalts-Snapshots deduplizieren. Geänderte Snapshots werden
separate unverifizierte Quellen; ein fortlaufendes Ersetzen oder Anhängen eines
vollständigen Transkripts wird nicht zugesichert. Deshalb Hermes `auto_retain`
auslassen und kurze Wissenseinträge gezielt speichern. Zu grosse Eingänge scheitern
explizit. Ein vom Upstream entfernter Operationsstatus liefert im nativen Gateway
404 entsprechend dem Plugin-Verhalten; Skirnirs Status-API nennt `not_found`.
Das ist kein Nachweis erfolgreicher Extraktion oder vollständiger Löschung.

Die englische Referenz dokumentiert die geprüften offiziellen API-/Plugin-
Quellversionen. Ein Test verwendet den echten Python-SDK (getestet 0.10.3) gegen
einen synthetischen HTTP-Server. Ein echter Hindsight-Modelltest und ein vollständiger
Hermes-Agent-Lauf sind damit ausdrücklich nicht durchgeführt.

## Persistente Outbox und Wiederanlauf

`router.hindsight.outbox_enabled: true` erfordert aktiviertes Hindsight und den
Session Store. Ohne Opt-in bleibt die bisherige flüchtige Queue verfügbar.
Im persistenten Modus wird der Auftrag vor der 202-Antwort von
`POST /v1/skirnir/memory/retain` in PostgreSQL committet. Ohne Datenbank gibt es
503 und keine falsche Dauerhaftigkeitszusage. Automatische Output-Erfassung bleibt
Best Effort: Eine erfolgreiche Inferenzantwort allein bestätigt keine Retention.
Für verbindliche Speicherung die explizite API und den Operationsstatus verwenden.

Stabile UUIDs und Inhalts-Fingerprints machen identische Wiederholungen idempotent;
abweichende Inhalte bei derselben Event-ID liefern 409. Shared-Operationen enthalten
die authentifizierte Owner-Identität. Worker verwenden `FOR UPDATE SKIP LOCKED`
und ablaufende UUID-Leases. Nach einem Absturz wird dieselbe Hindsight-Operation-ID
wiederverwendet. Das verhindert doppelte Verarbeitung mit der offiziellen Upstream-
Idempotenz; es ist keine verteilte Transaktion oder Exactly-once-Transportgarantie.

Defaults: Kapazität 1024, maximal fünf Claims, Lease 120 Sekunden, exponentielle
Wartezeit ab zwei Sekunden bis höchstens 3600 Sekunden. Danach `dead`.
Rechte-/Klassifikations-/Bank-Bindungsänderungen führen zu `revoked`.

Unter `/v1/skirnir/memory/operations/{uuid}`:

| Aktion | Bedeutung |
| --- | --- |
| `GET` | Nur eigener Status, Versuche und Zeitpunkte; nach Annahme tatsächlicher Upstream-Status. Kein Payload/Scope. |
| `DELETE` | Nur nie versendeten Auftrag abbrechen. Unklarer/in-flight Versand liefert 409. |
| `POST .../delete` mit `{}` | Separater `delete`-Grant, persistente gezielte Quellenlöschung. 202 wartend, 200 gelöscht. |

**Kuratoren eines gemeinsamen Bereichs.** Wer auf einem Projekt mit gemeinsamem `scope`
sowohl `read` als auch `delete` hat, kuratiert diesen Bereich:
- `POST /v1/skirnir/memory/list` mit `{project, type?, query?, limit?, offset?}` liefert eine
  Seite (1-50, Standard 20) aller Gedächtniseinheiten mit `document_id` (die Operation, aus der
  sie stammen), `source_memory_ids` und Herkunft. Was der Inhaltsfilter ablehnt, steht nur mit
  ID unter `withheld`.
- Status abfragen und Löschen gehen für jede Operation in der Bank dieses Bereichs, auch für
  fremde (nur REST; nie abbrechen oder neu senden, nie eine private Bank). Für alle anderen
  bleibt eine Operation besitzergebunden, eine fremde liefert 404.
- Jede Kurator-Löschung wird mit Kurator, Besitzer und Bereich protokolliert und als
  `outcome="curated"` gezählt. Der Lösch-Auftrag läuft danach unter dem Recht des Kurators;
  wird es entzogen, stoppen offene Löschungen.
- „Ändern“ heisst: korrigierten Fakt schreiben, alte Operation löschen. Hindsights
  direktes Bearbeiten ist nicht freigegeben.

Zustände: `pending`, `processing`, `accepted`, `completed`, `dead`, `revoked`,
`cancelled`, `delete_pending`, `deleted`. `accepted` bestätigt den Upstream-Eingang,
der Auftrag wird noch geprüft; `completed` heisst, Hindsight meldet die Extraktion fertig.

**Geprüfte Zustellung.** Hindsight wiederholt einen gescheiterten LLM-Aufruf nur kurz
(Standard drei Versuche, höchstens 60 s Abstand) und markiert die Operation dann
`failed`; ein stundenlang belegtes lokales Modell (Spiel auf dem GPU-Knoten) verliert so
die Extraktion. Die Outbox prüft jeden angenommenen Auftrag, zuerst nach
`verify_delay_s`, dann verdoppelt bis `verify_max_delay_s`. `completed` beendet ihn,
`failed`/`cancelled` reiht ihn nach `upstream_backoff_s` (verdoppelt, höchstens 6 h) unter
einer **neuen Upstream-Operations-ID** mit **gleicher Dokument-ID** neu ein, weil
Hindsight eine bekannte Operations-ID nur mit ihrem alten Zustand beantwortet. Höchstens
`upstream_attempts` Neueinreichungen je Runde, danach `dead` mit `upstream_failed`.
Unbekannte Operation: `dead` (`remote_unknown`); `verify_timeout_s` lang unbestätigt:
`dead` (`remote_pending`). Der Payload eines angenommenen Auftrags bleibt bis zur
Fertigstellung erhalten.

Vor gezielter Löschung muss Hindsight `completed` melden (oder `failed`, danach folgt
kein Schreiben mehr). Erst dann wird das
eigene unveränderliche UUID-Dokument gelöscht. Fehlender/unklarer Status bleibt
wartend beziehungsweise Dead Letter. Andere Quellen und die gemeinsame Bank werden
nicht pauschal gelöscht; die Rekonsolidierung verbleibender Quellen übernimmt
Hindsight. Backups, externe Kopien oder semantisch gleiche andere Quellen sind
von dieser Zusage nicht umfasst.

## Lebenszyklus und Aufbewahrung

`DELETE /v1/skirnir/sessions/{id}` entfernt nur die eigene Session samt Ereignissen,
Compactions, Import-Ledger und Scope-Referenzen; Erinnerungen werden separat verwaltet.
Alle Aufbewahrungsfristen sind mit null deaktiviert:

- `session_store.retention_days`: Alter seit letztem Session-Update.
- `session_store.archived_retention_days`: zusätzliche Frist für archivierte Sessions.
- `hindsight.outbox_retention_days`: Payloads abgeschlossener/terminaler Aufträge entfernen.

Die Wartung verarbeitet höchstens 100 Zeilen je Sweep und überspringt aktive
Managed-Turns. Outbox-Owner/Projekt, opaque IDs, Fingerprints, Scope-Bindung und
Zeitpunkte bleiben als Replay-Tombstones erhalten, damit Wiederholungen gelöschtes
Wissen nicht erneut anlegen. Die TTL löscht weder Upstream-Memory noch Backups.
Dafür separate dokumentierte Aufbewahrungs- und Wiederherstellungsregeln festlegen.
Tombstones nicht entfernen, solange alte Events erneut gesendet werden könnten.

## Session-Suche und Handoff v1

`GET /v1/skirnir/sessions` listet ausschliesslich eigene Sessions in freigegebenen
Projekten. Filter: `project`, `status=active|archived`, `updated_since` mit Zeitzone,
`q` als Projekt-Teilstring, `limit` 1..100 (Default 50), `before` als zurückgegebener
Cursor. Listings enthalten keine Gesprächsinhalte oder Metadaten. `next_before`
führt zur nächsten Seite, absteigend nach `(updated_at,id)`; gleichzeitige Updates
können Einträge zwischen Seiten verschieben.

`session_store.handoff_enabled` separat aktivieren und einen externen Signaturschlüssel
mit mindestens 32 Bytes setzen. `GET /v1/skirnir/sessions/{id}/handoff` exportiert:

| Feld | Inhalt |
| --- | --- |
| `schema_version`, `kind` | `1`, `skirnir-agent-handoff` |
| `source` | Herkunfts-Client, Session, Projekt, Datenklasse, Modus, Aktualisierung |
| `context` | Aktiver Verlauf, Zusammenfassung, Compaction-Version, Original-Cursor, Checkpoint |
| `work` | Offene Schritte, Repo/Branch/Commit/Artefaktreferenzen, beobachtete Memory Scopes, offene Tool-Aufrufe |
| `observed` | Letzter persistierter Turn-Zustand |
| `issued_at`, `expires_at`, `signature` | 24 Stunden Gültigkeit, HMAC-SHA256 |
| `resumable_execution` | Immer `false` |

Session-Metadaten `handoff` können saubere HTTP(S)- oder relative/repo-Referenzen
tragen. Keine Credentials, URL-Querys, absoluten Pfade oder Traversal; referenzierte
Dateien werden nicht gelesen. Eine Heuristik verweigert möglicherweise sensible
Exporte, ist aber kein universeller Secret-Detektor. Pakete sind signiert, nicht
verschlüsselt; nur über vertrauenswürdige authentifizierte Kanäle weitergeben.

`POST /v1/skirnir/sessions/import` erhält `package`, Ziel-`project`, stabile
`event_id` und optional `mode` (`client-managed`, alternativ aktiviertes
`skirnir-managed`). Signatur, Ablaufzeit, existierende Quell-Session und aktuelle
Projektfreigaben werden geprüft. Für fremde Herkunft braucht der Empfänger
`session_handoff_from: [source-client]`. Die Datenklasse kann nicht abgesenkt werden.
Import erzeugt atomar eine **neue eigene Session**; gleicher Event/Inhalt liefert
dieselbe Session, geänderter Replay 409. Schlüsselrotation, Rechteentzug oder
Quelllöschung können ein Paket ungültig machen.

Importierte Nachrichten und Tool-Beschreibungen werden untrusted User-Kontext.
Keine Systemautorität, Ausführung, Prozesswiederherstellung oder Memory-Grants
werden übernommen. Scope-Referenzen bleiben Hinweise. Der letzte Checkpoint ist
auch über `GET /v1/skirnir/sessions/{id}/checkpoints/latest` explizit abrufbar.

## Kontext, Checkpoints und Wissensqualität

`context_manager.optimize_recall` begrenzt Recall auf `recall_budget_percent`
(Default 20) und entfernt bei Bedarf nachrangige Einträge zugunsten geschützten
Verlaufs. Vollständige Untrusted-Wrapper bleiben erhalten. Die Schätzung nutzt
`context_check`, konservative UTF-8-/Tool-Overheads, Antwort- und Sicherheitsreserve;
sie ersetzt keinen exakten Modell-Tokenizer.

`reduce_tool_results` reduziert nur alte abgeschlossene Tool-Ergebnisse in der
Projection. Rolle, IDs, Paarung und Quellmarker bleiben erhalten. Systeme/Developer,
aktueller Tail, Checkpoints und offene Eingaben bleiben exakt. Originale und alte
Compaction-Versionen werden nie umgeschrieben. Passt geschützter Kontext nicht,
folgt kontrolliert 413. Es gibt keine zusätzlichen LLM-/Cloud-Aufrufe.

`auto_checkpoints` speichert nur beobachtbaren Gesprächsfortschritt atomar mit einem
abgeschlossenen Managed-Turn und dedupliziert identische Payloads. Explizite Ziele,
offene Arbeit und Artefakte bleiben erhalten; externe Arbeit und offene Tools sind
unbestätigt. Ganze Antworten, Arbeitsverzeichnisse und Secrets werden nicht als
Checkpoint kopiert. Tatsächlichen semantischen Task-Fortschritt muss der Agent
weiterhin ausdrücklich beschreiben.

Memory-Provenienz enthält Art (`user_assertion`, `confirmed_information`,
`assistant_derivation`), Herkunft, Zeit, Bestätigung, Version, Quellen und optional
`supersedes`. Explizites Skirnir-Retain verlangt weiterhin `confirmed: true`;
automatisches beziehungsweise Provider-Retain bleibt unverifizierte Ableitung.
Widersprüchliche Quellen werden nicht automatisch überschrieben. `supersedes` ist
ein nachvollziehbarer Hinweis, keine pauschale Löschung. Recall bleibt untrusted
und darf keine höher priorisierten Anweisungen ersetzen. Optionaler
`recall_min_score` nutzt Reranker-Scores; oberhalb null werden auch unbewertete
Ergebnisse ausgeschlossen. Es entsteht keine zweite Knowledge-Graph-Engine.

## Betrieb, Tests und Rollback

Migration **3** ergänzt Zeitstempel/Indizes/Trigger, Import-Idempotenz, Scope-
Referenzen und Outbox. Veröffentlichte Migrationen 1/2 bleiben unverändert.
Der bestehende Migrator führt sie beim Start transaktional aus. Vor Upgrade ein
extern verwaltetes Backup erstellen; Integrationstests nur auf Wegwerfdatenbanken.

Die geschützte Control-API `/admin/context` und Overview-Kacheln zeigen nur
aggregierte Zustände von Session Store, Context Manager, Hindsight und Outbox.
Hindsight bleibt bis zur ersten echten Anfrage `unknown`. Prometheus erfasst
Recall-/Retain-Latenzen und Fehler, Outbox-Zustände, Kontextschätzungen/-einsparungen,
Compactions, Handoffs und Backend-Verfügbarkeit. Labels enthalten keine Clients,
Sessions, Banks oder Inhalte. Die englische Referenz listet die Metriknamen.

403 bedeutet fehlende/entzogene Freigabe; 409 Event-Konflikt, Lease oder unklaren
Versand; 413 ein Grössenlimit; 503 fehlenden Dienst oder Kapazität. Dead Letters
gezielt untersuchen, nicht blind unter Umgehung der Rechte erneut versenden.
Neustart nimmt berechtigte wartende/abgelaufene Aufträge wieder auf.

Ruff, reine Konfigurationsprüfung, alle Selftests, Benchmark und E2E ausführen.
PostgreSQL-CI führt alle drei Integrationsskripte gegen PostgreSQL 16 aus.
Der optionale echte SDK benötigt `test/requirements-hindsight.txt`.
Der synthetische Benchmark prüft Budgets 4096/8192/16384, je 20 Projektionen,
Guard-Marker, Tool-Paare, stabile Hashes und Wechsel auf kleinere Fenster.
Seine konservative Grösse sinkt von 83595 auf 2495 (97,02 %). Das ist eine
Grössenmessung, keine Garantie semantischer Vollständigkeit oder Modellqualität.
Laufzeitwerte sind nur informativ. Separate Tests prüfen Recall-Scores/Provenienz.
Echter Hindsight-Dienst und vollständiger Hermes-Lauf bleiben separat zu prüfen.

Rollback: neuen Traffic und Worker anhalten, neue Optionen ausschalten und die
vorherige Client-Konfiguration wiederherstellen; alte Versionen lehnen neue Keys ab.
Persistente Aufträge zuerst abarbeiten oder ausdrücklich pausieren und die DB
behalten. Nicht unbemerkt auf die flüchtige Queue wechseln. Neue Funktionen im aktualisierten Build auszuschalten erhält das additive Schema
sicher. Ein älteres Binary, dessen Migrator nur Version 1/2 kennt, verweigert
Schema-Version 3. Ein Code-Downgrade benötigt daher einen geprüften Kompatibilitätsbuild
mit Kenntnis von Migration 3 oder einen separat geplanten Restore der vorherigen DB.
Versionsstand niemals manuell herabsetzen; weder Migrationen 1/2 ändern noch das
Schema als Abkürzung löschen. DB-Restore benötigt einen eigenen Recovery-Plan
einschliesslich Tombstones und Upstream-Idempotenz; er macht Hindsight-Seiteneffekte
nicht rückgängig.
