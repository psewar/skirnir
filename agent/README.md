# skirnir-agent (Skirnir-Agent: Windows-Dienst, Linux-Binary)

Eine .exe (Go, statisch) ersetzt auf einem GPU-Rechner die Dauerlauf-Scheduled-Tasks: Heartbeat an den Router,
MQTT-Geraet fuer Home Assistant, Aufsicht ueber lokale KI-Dienste (z. B. einen Wyoming-STT-Server als Kindprozess).

| Datei | Zweck |
|---|---|
| `*.go` | `main` (Verben), `app` (Verdrahtung), `config`, `logging`, `gpu` (NVML direkt aus `nvml.dll`, Rueckfall nvidia-smi; `gpu_nvml_windows.go`), `sensors` + `gpuz_*` (Sensoren, GPU-Z), `guard` (GPU-Schutz), `kuerzung` (Kuerzungsmeldung), `heartbeat`, `tunnel`, `identity`, `provision`, `facts`, `updater*` (Selbst-Update), `ollamaupdate*` (Ollama-Update ueber den Router), `proxy` (TLS-Vorschaltstelle), `secrets` (Secret-Store, Universal-Auth-API), `mqtt`, `supervisor` (Kinder + Job-Objekt), `health`, `service` (SCM), `install` |
| `build.sh` | Cross-Compile aus WSL: `wsl -e bash -lc '/mnt/c/<workspace>/skirnir/agent/build.sh 0.1.1'` → `dist/skirnir-agent.exe` |
| `config.example.yaml` | Vorlage fuer neue Rechner **mit Installationsanleitung Windows/Linux im Kopf**; die echte Datei liegt unter `C:\ProgramData\skirnir-agent\config.yaml` |
| `skirnir-agent.service` | systemd-Unit fuer Linux-Knoten (`run --config /etc/skirnir-agent/config.yaml`) |
| `Install-Service.ps1` | Sonderfaelle (alte Tasks entfernen, Firewall-Regeln des Ollama-Installers); die normale Einrichtung ist seit 0.10.0 `setup` (Doppelklick auf die Exe) |

## Umbenennung (0.11.0)

Binary `skirnir-agent.exe`, Programmordner `C:\Program Files\skirnir-agent`, Konfiguration `C:\ProgramData\skirnir-agent\config.yaml`,
Dienst `SkirnirAgent` ("Skirnir Agent"), Aufgabe `SkirnirAgent-GpuzRelay`, Linux `/usr/local/bin/skirnir-agent` und
`/etc/skirnir-agent`. Ein Knoten, der sich unter dem alten Namen selbst aktualisiert hat, laeuft weiter: fehlt die neue
Konfiguration, gilt die alte unter `ollama-router-agent`; Dienst `OllamaRouterAgent` und Aufgabe `OllamaRouterAgent-GpuzRelay`
werden als Rueckfall gefunden. `setup` zieht einmalig um: Verzeichnis verschieben, Pfade in der Config, alter Dienst und alte
Aufgabe weg, neuer Dienst, alter Programmordner weg. MQTT-Entitaeten aendern sich nicht (Geraete-ID bleibt).

## Einrichtung per Doppelklick (0.10.0)

`skirnir-agent.exe` ohne Argumente (Doppelklick) oder mit `setup`: die Binary hebt sich per UAC selbst an (Pruefung
ueber das Token, nicht ueber den lokalisierten Gruppennamen - `Install-Service.ps1` verweigerte auf einem deutschen Windows
mit "Administrators"), fragt beim ersten Mal nach Router-Adresse, Update-Schluessel des Betreibers und dem Benutzer mit
Steuerrecht, schreibt `config.yaml` (LocalSystem, Anzeigename "Skirnir Agent"), kopiert sich nach `Program Files`, legt den
Dienst an oder aktualisiert ihn in place (Konto geaendert -> `uninstall` + `install`, Identitaet und Logs bleiben), legt die
GPU-Z-Relay-Aufgabe an und zeigt `/health` samt Fingerprint. Bei vorhandener Konfiguration ergaenzt es auf Nachfrage den
Update-Schluessel und stellt ein altes `NT SERVICE`-Konto auf LocalSystem um. `install`, `apply-rules` und `start` ruft es
ueber die installierte Binary auf, damit der Dienst im SCM auf `Program Files` zeigt. Ein eigenes Konsolenfenster (Doppelklick,
UAC) wartet am Ende auf Enter (`setup_windows.go`).

## Schlüssel-Identität und Registrierung (0.4.0)

Beim ersten Start erzeugt der Agent ein Ed25519-Schlüsselpaar (`identity.dir`, Windows: DPAPI-geschützt) und meldet sich
am Router mit signierter Challenge und seinen Fakten (`facts.go`: Hostname, OS, Hersteller/Modell, GPU, VRAM, MAC der
Route zum Router, Ollama-Version). Der Router legt den Knoten als "wartet auf Freigabe" an; nach der Freigabe kommt das
Konfigurationspaket (`provision.go`: MQTT-Zugang, Heartbeat-Intervall) durch den Tunnel und wird lokal gespeichert. Die
lokale Config braucht nur `router.url`; `node` (Standard Hostname) und `children` sind optional, `mqtt:` nur zum
Überschreiben oder Abschalten. `identity` zeigt den Fingerprint zum Abgleich mit der UI. Kein Token mehr.

Plattformen: alles Windows-Spezifische (Dienst, Job-Objekt, DPAPI, Registry, Firewall) liegt in `platform_windows.go` und
`install.go`/`service.go` (Build-Tag windows); `platform_other.go` liefert Linux/macOS-Stubs (Prozessgruppe statt Job,
Schlüssel als 0600-Datei, `run` per systemd). `build.sh` baut Windows, `GOOS=linux go build` die Linux-Binary
(`dist/skirnir-agent-linux-amd64`).

## Tunnel (0.3.0, Normalweg)

Ausgehende WebSocket-Verbindung zum Router (`/v1/tunnel`, Anmeldung mit der Ed25519-Identitaet per Challenge, kein Token),
seit 0.9.1 der einzige Weg (der HTTP-Heartbeat mit `router.token` und `tunnel.enabled` sind entfernt); der
Router ruft Ollama hindurch auf (`tunnel.upstream`, Standard `http://127.0.0.1:11434`). Keine eingehende Firewall-Regel, kein
Zertifikat, keine feste IP; Reconnect mit Backoff 1-30 s; `/health` zeigt `tunnel`. Ein neuer Rechner braucht nur die Binary,
die Config (node, router.url; optional secret_store fuer ein MQTT-Passwort aus dem Secret-Store) und `Install-Service.ps1`.

## TLS-Vorschaltstelle (0.2.0, Alternative ohne Tunnel)

`ollama_proxy` startet einen HTTPS-Reverse-Proxy vor dem lokalen Ollama (Standard 0.0.0.0:11443 → 127.0.0.1:11434).
Selbstsigniertes ECDSA-Zertifikat in `cert_dir` (beim ersten Start erzeugt), Fingerprint im `/health` und im Heartbeat
(`ollama_tls_sha256`), Token-Pflicht (`X-Router-Token` = Router-Token, sonst 401), Streaming ohne Pufferung. Firewall-Regel
in `install.firewall` mit `remote: [192.0.2.10]`. MQTT über TLS: `mqtt.tls: true`, Host per Name (Zertifikat), Port 8883.
`apply-rules` setzt ACLs/Firewall/Steuerrecht nach Config-Änderungen neu (Admin); `Install-Service.ps1 -RemoveOpenOllamaRules`
entfernt die offenen `ollama.exe`-Regeln des Ollama-Installers.

## Verben

```
skirnir-agent.exe run --config <pfad>     Konsole (Tests), Ctrl-C beendet
skirnir-agent.exe status                  Dienstzustand + /health
skirnir-agent.exe start|stop|restart      ohne UAC (Installer setzt das Steuerrecht fuer <user>)
skirnir-agent.exe install|uninstall       Admin
skirnir-agent.exe check-config | mqtt-clear | version
skirnir-agent.exe gpu                     Messquelle und alle Sensoren einmal ausgeben (auch GPU-Z, falls es laeuft)
```

`/health` auf `127.0.0.1:10398`: GPU, Heartbeat (Router-Urteil), MQTT, Kinder. `POST /restart-child?name=stt` startet ein Kind neu.

## HA-Entitaeten: nur die, die es hier gibt (seit 0.4.1)

Der Agent legt sein HA-Geraet per MQTT-Discovery an. Drei Entitaeten gibt es nicht auf jedem Rechner, sie werden
darum nur veroeffentlicht, wenn sie hier etwas bedeuten (`sensorGroup` in `mqtt.go`):

| Entitaet | Bedingung |
|---|---|
| `STT-Dienst` | ein Kindprozess mit dem Namen aus `mqtt.stt_child` (Standard `stt`) ist konfiguriert |
| `Alias-Modell`, `Alias-Modell strukturfaehig` | `mqtt.alias_model` ist gesetzt und die lokale Ollama kennt dieses Modell |

`syncDiscovery` gleicht bei jedem Connect und jedem Melde-Zyklus ab: fehlende Configs anlegen, ueberzaehlige mit
leerem Payload loeschen. Eine Gruppe darf also im Betrieb dazukommen (Modell nachtraeglich gepullt) oder wegfallen.
Wichtig: **nur eine sichere Auskunft entscheidet**. Antwortet Ollama mit 404, gibt es das Alias-Modell hier nicht;
ist Ollama gar nicht erreichbar, bleibt alles wie es war -- sonst wuerden bei jedem Ollama-Neustart Entitaeten
verschwinden und wiederkommen. Solange eine Gruppe unbekannt ist, wird weder angelegt noch geloescht.
Nebeneffekt: ohne STT-Kind entfaellt auch der Portversuch auf 10300 in jedem Zyklus.

Anlass war ein zweiter Knoten: dort standen `STT-Dienst` und `Alias-Modell strukturfaehig` dauerhaft `off`,
weil beides nur auf dem ersten Knoten existiert. Tests dazu in `mqtt_test.go` (`go test ./...`).

## Selbst-Update ueber den Router (0.8.0)

Knoten ohne Fernzugriff (nur ausgehender Tunnel) und wachsende Geraetezahl: der Router verteilt Agent-Binaries, der Agent
holt sie sich und tauscht sich selbst (`updater.go`; Router `agentupdate.py`).

1. `deploy.py --agent` (Arbeitsstation) legt die Binaries aus `agent/dist/` mit einem **Manifest** (Version, Datei je
   OS/Arch, SHA-256) nach `/etc/skirnir-router/agent/`. Das Manifest ist mit dem **Betreiber-Schluessel** signiert (Ed25519,
   `agent-update.key` im Ops-Ordner, beim ersten Aufruf erzeugt). Der Router selbst kann nichts signieren: ein
   kompromittierter Router kann Inferenz umlenken, aber keinen Code auf die Knoten bringen.
2. Der oeffentliche Schluessel gehoert in die Agent-Konfiguration: `update: {public_key: <base64>}`. Ohne ihn lehnt der
   Agent jeden Auftrag ab. `update: {enabled: false}` schaltet das Selbst-Update ab.
3. Auftrag: Knopf in der Router-UI (Registrierte Agenten) oder die Rollout-Schleife (Policy **Auto-Update** je Knoten;
   `modes.agent_update.canary` bekommt neue Versionen zuerst, die anderen erst, wenn der Kanarienvogel sie
   `canary_clean_h` Stunden faehrt). Nur im Leerlauf; der Knoten bekommt waehrenddessen keine neuen Anfragen.
4. Der Agent prueft **erst die Signatur** ueber das mitgeschickte Manifest, dann ob Datei, OS, Arch und SHA-256 darin
   stehen, laedt die Binary mit einem Einmal-Token (15 min) neben sich (`<exe>.new`), prueft den Hash, laesst sie
   `version` sagen, benennt die laufende Datei in `<exe>.old` um, setzt die neue ein und startet den Dienst neu
   (Windows: `restart` aus einem vom Job losgeloesten Prozess; Linux: Ende, systemd `Restart=always`). Beim naechsten
   Start raeumt er `.old` weg und startet die GPU-Z-Relay-Aufgabe neu, die noch aus der alten Datei laeuft.
5. Stand geht im Heartbeat mit (`update: {state, version, message}`): `downloading`, `applied`, `failed`. Meldet sich
   der Agent mit der neuen Version, gilt der Auftrag als `done`. Fehler oder 15 min Stille -> HA-Problem im Router.

Tests: `updater_test.go` (Signatur fremd/veraendert, Hash-Widerspruch, fremde Datei, ohne Schluessel, abgewaehlt).

## Ollama-Update ueber den Router (0.12.0)

Laeuft Ollama als Kind des Dienstes (`children:` mit `ollama.exe`), greift der Updater der Tray-App nicht mehr: er laeuft
unter dem Desktop-Benutzer, und `OllamaSetup.exe` ist ein Pro-Benutzer-Installer (unter LocalSystem landete er im
SYSTEM-Profil). Der Router uebernimmt die Rolle des Updaters (`ollamaupdate.go`; Router `ollamaupdate.py`).

1. Der Router prueft alle `check_interval_h` Stunden das neueste Release (GitHub-API, `sha256sum.txt`) und zeigt es je
   Knoten. Auftrag: Knopf in der UI oder Rollout-Schleife (Policy **Ollama-Auto-Update** je Knoten, Nachtfenster
   `modes.ollama_update.window_start..window_end`, Kanarienvogel zuerst, nur wenn der Knoten frei ist). Durch den Tunnel
   kommt `{"t":"ollama-update", version, file, sha256, size}`.
2. Der Agent nimmt den Auftrag nur an, wenn Ollama sein Kind ist und `file` das Archiv fuer dieses System ist
   (`ollama-windows-amd64.zip`; Linux-Archive sind tar.zst und noch nicht unterstuetzt). Er holt `sha256sum.txt` von
   seiner **eigenen festen Quelle** (`ollama_update.source`, Standard GitHub-Releases von ollama/ollama) und vergleicht
   mit dem Auftrag: der Router kann nur die Version waehlen, nicht den Code.
3. Download (ca. 1,4 GB, Fortschritt im Heartbeat) in `<Programme>\.skirnir-ollama-update\`, SHA-256, entpacken (Zip-Slip
   abgefangen), `ollama --version` der entpackten Binary muss die Zielversion nennen. Vorher: dreifacher Archivplatz frei.
4. Tausch: Supervisor haelt das Kind an (`Hold`, Prozessbaum samt Modellprozessen), `ollama.exe` und `lib` wandern in eine
   Sicherung, die neuen Dateien an ihren Platz, `Release` startet das Kind sofort neu. `/api/version` muss binnen 3 min die
   Zielversion melden, sonst Rollback auf die Sicherung. Tray-App und Windows-Deinstallationseintrag bleiben alt (kosmetisch).
5. Stand im Heartbeat (`ollama_update: {state, version, message}`): checking, downloading, extracting, swapping, applied,
   failed; dazu `ollama_version` alle 60 s frisch. Meldet der Knoten die Zielversion, gilt der Auftrag als `done`.

Konfiguration: `ollama_update: {enabled: false}` schaltet es ab, `source:` aendert die Quelle (z. B. ein interner Spiegel
mit derselben Ordnerstruktur `v<version>/<datei>` + `sha256sum.txt`).
Tests: `ollamaupdate_test.go` (Pruefsummen-Datei, Versionsausgabe, Auftragspruefung, Zip-Slip, Tausch + Rollback, ganzer
Ablauf gegen eine Fake-Quelle mit Erfolg und mit ausbleibender Neuversion).

## GPU-Schutz (0.7.0)

Hintergrund und Abwaegung: [design/gpu-guard.md](../design/gpu-guard.md). Der 12V-2x6-Stecker einer RTX 5090 fuehrt bei
575 W rund 48 A ohne Einzelpin-Messung; Software sieht einen schlechten Kontakt nicht, kann aber den Gesamtstrom senken,
Dauer-Vollast begrenzen und frueh warnen. Der Agent tut genau das (`guard.go`, reine Zustandsfunktion, Tests in
`guard_test.go` mit Karten-Attrappe):

| Zustand | Bedeutung |
|---|---|
| `normal` | Dauerlimit gesetzt (Standard 80 % des Standardlimits, 5090: 460 W) |
| `hochlast` | Leistung >= 90 % des aktiven Limits seit der halben Frist (300 s) - Vorwarnung |
| `gedrosselt` | 600 s am Limit -> Stufe 2 (70 %, 5090: 403 W) fuer 300 s; Luecken bis 15 s (`high_load_gap_s`) unterbrechen den Zaehler nicht (0.7.1 - vorher setzte jede Warteschlangen-Luecke ihn zurueck, gemessen 2026-09-25) |
| `erholung` | zurueck auf das Dauerlimit, 60 s lang gemeldet |
| `unverfuegbar` | Limit nicht lesbar oder nicht setzbar (kein NVML, keine Berechtigung, Linux ohne root) - nur Beobachtung, **Problem** |
| `aus` | `gpu_guard.enabled: false` - kein Limit; zaehlt auf Wunsch des Betreibers als **Problem**, damit eine vergessene Abwahl auffaellt |

Regeln: der Guard **senkt nur** (ein Fremdtool, das tiefer stellt, bleibt), hebt nur von einem selbst gesetzten Wert
(Stufe 2 -> Dauerlimit) und geht **nie ueber das Standardlimit**; jedes Ziel ist auf [Min-Limit, Standardlimit]
geklemmt. Das Limit ist fluechtig (Treiber-Neustart) und wird alle `reapply_s` (30 s) geprueft. Steht es dreimal
hintereinander wieder hoeher, meldet er `fremdeingriff`. Warnungen unabhaengig vom Limit: `spannung_niedrig` (16-Pin
unter Last < 11,6 V oder > 0,35 V unter der gelernten Leerlauf-Referenz, 30 s Bestand; nur mit GPU-Z), `temperatur_hoch`
(Speicher >= 95 °C, Hot Spot >= 100 °C), `hw_drossel` (NVML meldet hw_slowdown/hw_thermal/hw_power_brake).

```yaml
gpu_guard:                    # alles optional; fehlende Werte = Standard
  enabled: true               # false = ausdruecklich abgewaehlt (Problem in HA und Router)
  power_limit_pct: 80         # Dauerlimit in % des Standardlimits; power_limit_w: 450 waere die absolute Form
  reapply_s: 30
  high_load_pct: 90
  high_load_s: 600
  high_load_gap_s: 15        # kurze Einbrueche zwischen Anfragen zaehlen nicht als Unterbrechung
  stage2_pct: 70
  recovery_s: 300
  voltage_warn_v: 11.6
  voltage_drop_v: 0.35
  voltage_hold_s: 30
  voltage_load_w: 300         # Spannung zaehlt erst ab dieser 16-Pin-Leistung
  mem_temp_warn_c: 95
  hotspot_warn_c: 100
```

Sichtbar: Heartbeat-Block `gpu_guard` (Zustand, Quelle, Leistung, 16-Pin, Limits, Ziel, Throttle, Warnungen), `/health`,
HA-Entitaeten `GPU-Schutz` (Zustand mit Attributen), `GPU-Schutz Problem`, `GPU-Schutz Ziel-Limit` und das Ereignis
`GPU-Schutz Ereignis` (Topic `<device>/gpu_guard`). Die Heartbeat-Antwort des Routers traegt `gpu_guard_ack`: ob der
Router den Status dieses Knotens beachtet (Deckel 1 in Stufe 2, Score-Abzug bei Hochlast, Probleme an HA); Attribut
`router_beachtet` in HA. Trockenlauf ohne Setzen: `skirnir-agent.exe gpu` zeigt Limits und Ziele.
Setzen braucht Adminrechte auf die GPU (NVML rc 4 = verweigert): als **LocalSystem** (`install.account: ''`) hat der
Dienst sie, das virtuelle Dienstkonto `NT SERVICE\...` nicht (gemessen 2026-09-24: rc 4, Guard `unverfuegbar`). Wer beim
virtuellen Konto bleibt, hat Beobachtung und Warnungen, aber kein Limit. Unter Linux braucht `nvidia-smi -pl` root.

## Sensoren (0.6.0)

Bis 0.5.x kannte der Heartbeat nur GPU-Auslastung und VRAM. Seit 0.6.0 liest der Agent zusaetzlich (`sensors.go`):

| Quelle | Werte | Bedingung |
|---|---|---|
| NVML (Windows) bzw. `nvidia-smi` (Linux, Rueckfall) | Temperatur, Leistung, wirksames und Standard-Power-Limit, Luefter, Speichercontroller-Last, Drosselgruende (`clocks_event_reasons`-Bitmaske, dekodiert: `sw_power_cap`, `hw_thermal`, ...) | immer, auch als Dienst ohne Anmeldung; jeder Wert einzeln optional |
| GPU-Z Shared Memory (`GPUZShMem`, nur Windows) | Speichertemperatur, Hot Spot, GPU-Spannung, 16-Pin-Leistung und -Spannung, Board Power, PerfCap-Grund, CPU-Temperatur | GPU-Z laeuft (Tray reicht); `gpuz.enabled: false` schaltet den Leser ab |

Der Block `sensors` haengt an jedem Heartbeat und am `/health`-GPU-Sample; der Router legt ihn unveraendert in den
Knotenzustand (`/admin/state` -> `nodes.<n>.sensors`). Das MQTT-Geraet macht daraus HA-Sensoren in zwei Gruppen:
`gpu` (NVML-Werte) und `gpuz`. Beide Gruppen entstehen, sobald einmal Werte da waren, und bleiben dann - wird GPU-Z
geschlossen, stehen die Entitaeten auf *unbekannt* statt zu verschwinden und beim naechsten Start wiederzukommen.

GPU-Z legt sein Objekt in der Anmeldesitzung des Benutzers an (`\Sessions\<n>\BaseNamedObjects\GPUZShMem`), ohne
`Global\`, mit einer DACL fuer SYSTEM, Administratoren und die eigene Anmeldesitzung (GPU-Z laeuft erhoeht). Der Dienst
versucht zuerst, es ueber den vollen NT-Pfad zu oeffnen (`NtOpenSection`, `gpuz_windows.go`) - das gelingt als LocalSystem.
Mit dem virtuellen Dienstkonto (Standard bis 0.6.0; seit dem GPU-Schutz ist LocalSystem der Normalfall) `NT SERVICE\SkirnirAgent` ist der Zugriff verweigert; dafuer gibt es das
**Relay**: `Install-Service.ps1` legt die Aufgabe `SkirnirAgent-GpuzRelay` an (bei Anmeldung, Gruppe Benutzer, ohne
Adminrechte, versteckt, `conhost --headless`), die `skirnir-agent.exe gpuz-relay` startet. Das Relay liest den Block
in der Anmeldesitzung und schickt die Whitelist alle 2 s per `POST /gpuz` an den Health-Port (nur localhost); der Dienst
nimmt die Werte 15 s lang als frisch. Ohne GPU-Z wartet das Relay still (Versuch alle 30 s). `-SkipGpuzRelay` legt die
Aufgabe nicht an bzw. entfernt sie; `gpuz.enabled: false` im Dienst lehnt Relay-Werte mit 409 ab.
Steht `lastUpdate` 15 s still, gilt der Block als veraltet und wird losgelassen; alle 30 s wird neu probiert. Von den
statischen GPU-Z-Daten (Karte, BIOS, Monitor samt Seriennummer) wird nichts uebernommen - nur die Sensor-Whitelist in
`gpuz_parse.go`. Tests: `gpuz_test.go` (synthetischer Block, NaN, PerfCap-Bits, kein statischer Wert im MQTT-Zustand).

## Kuerzungsmeldung (0.5.3)

Ollama kuerzt zu lange Prompts **still**: der Client bekommt eine normale Antwort, nur fehlt ihm ein Teil des
Verlaufs. Die einzige Spur steht in Ollamas eigenem Log. Der Agent liest die Zeilen mit, die der Supervisor ohnehin
ins Kind-Log schreibt (`kuerzung.go`), und erkennt zwei Muster:

| Art (`event_type`) | Log-Zeile | Bedeutung |
|---|---|---|
| `eingabe_gekuerzt` | `msg="truncating input prompt" limit=... prompt=... new=...` | Prompt vor der Verarbeitung abgeschnitten |
| `kontext_voll` | `stop processing: n_tokens = N, truncated = 1` | Kontextfenster lief waehrend der Verarbeitung voll |

In HA erscheinen drei Entitaeten: `Ollama Kürzungen` (Zaehler seit Dienststart, `total_increasing`),
`Ollama letzte Kürzung` (Zeitstempel, Details als Attribute) und das Ereignis `Ollama Kürzung` fuer Automationen
(Topic `<device_id>/kuerzung`, nicht retained, sofort bei der Kuerzung statt im Melde-Intervall). Die Zeile landet
zusaetzlich als Warnung im `agent.log`. Gegenprobe: `SKIRNIR_OLLAMA_LOGS=<logordner>` und der Test
`TestEchteOllamaLogs` zaehlt die Treffer in vorhandenen Logs (PSEWAR-2026 bis 2026-09-24: 1 + 7 in 449 487 Zeilen,
kein Treffer in den 5 132 Normalzeilen `truncated = 0`).

## Betrieb

- Logs: `C:\ProgramData\skirnir-agent\logs\agent.log`, `stt.log` und `ollama.log` (rotiert, 10 × 5 MB); Start/Stopp auch im
  Windows-Ereignisprotokoll (Quelle `SkirnirAgent`).
- Dienstkonto: **LocalSystem** (`install.account: ''`, Normalfall seit dem GPU-Schutz) oder das virtuelle Konto
  `NT SERVICE\SkirnirAgent` (nur Beobachtung). Der Installer gibt dem Konto Lesen auf die Pfade in `grant_read`, Aendern
  auf `grant_modify` und `ProgramData\skirnir-agent`, und schottet das Config-Verzeichnis ab (SYSTEM, Administratoren,
  Dienstkonto). `allow_control_users` bekommen seit 0.9.0 nur das Dienst-Steuerrecht, kein Verzeichnisrecht mehr: dort liegen
  Identitaetsschluessel, Provisionierung (MQTT-Passwort) und `control.token`, das Token fuer `POST /restart-child`
  (Header `X-Agent-Token`) am Health-Port.
- **Ollama als Kind des Dienstes:** `ollama.exe serve` laeuft als `children`-Eintrag, nicht
  mehr als Tray-App aus dem Startup-Ordner. Grund: die Tray-App startet erst mit der Anmeldung, und ihr Autostart-Eintrag
  kann deaktiviert sein - dann kommt Ollama nach einem Reboot nie hoch und der Knoten bleibt fuer
  Skirnir offline, obwohl Agent und Tunnel laufen. Was dazu gehoert: Benutzer-Umgebungsvariablen von <user> gelten fuer das
  Dienstkonto nicht, darum stehen `OLLAMA_HOST/MODELS/NUM_PARALLEL/FLASH_ATTENTION/CONTEXT_LENGTH/LOAD_TIMEOUT` als `env`
  am Kind; `OLLAMA_MODELS` zeigt auf den bestehenden Modellspeicher (Dienstkonto `(OI)(CI)M`, `ollama pull` schreibt dort weiter),
  `USERPROFILE` auf `ProgramData\skirnir-agent\ollama-home` (eigener `~/.ollama`-Schluessel des Dienstes); Lesen
  auf `AppData\Local\Programs\Ollama`. Beides steht in `grant_read`/`grant_modify`, `apply-rules` setzt es neu.
  Verifiziert: CUDA aus dem Dienstkontext (granite4.2:8b 100 % im VRAM), `ollama cp`/`rm` schreibt, Router meldet den
  Knoten 4 s nach dem Dienststart free. **Die Tray-App `ollama app.exe` darf daneben NICHT laufen:** sie verbindet sich
  nicht mit einem fremden Server, sondern versucht sekuendlich einen eigenen zu starten (`ollama exited exit status 1`
  in `app.log`). Ihr Startup-Eintrag ist deshalb deaktiviert (StartupApproved `03`). Ollama-Update: Dienst stoppen,
  Installer/`winget upgrade Ollama.Ollama`, Dienst starten - der Installer legt `Ollama.lnk` neu an, der deaktivierte
  StartupApproved-Wert bleibt.
- Recovery: Neustart nach 5 s / 30 s / 60 s, Zaehler-Reset taeglich. Kinder haengen an einem Job-Objekt und sterben
  mit dem Dienst (getestet: harter Kill des Dienstes nimmt den STT-Prozess mit, LWT setzt das HA-Geraet sofort offline).
- Secrets: MQTT-Passwort zur Laufzeit aus Secret-Store (Identity `gpu-desktop-runtime`, viewer auf Projekt router-host; Creds
  in `<secrets-dir>\gpu-desktop-runtime.env` und in der Config). Router-Token steht in der Config.
- Update: ueber den Router (Abschnitt Selbst-Update); von Hand: neue Binary bauen, `Install-Service.ps1` als Admin (erkennt den
  bestehenden Dienst: stop, kopieren, start). Auf Knoten ohne Kindprozesse reicht die Binary plus die Config; `grant_read`
  und `firewall` bleiben dort leer. Linux: die Unit gibt `/usr/local/bin` frei (`ReadWritePaths`), sonst kann der Updater
  die neue Binary nicht daneben legen.
- Stirbt ein Modul mit Panic (Tunnel, Guard, MQTT ...), beendet sich der Dienst mit Fehler statt als gesunder Zombie
  weiterzulaufen; Recovery (Windows) bzw. `Restart=always` (systemd) starten ihn neu.
- Rueckbau: `skirnir-agent.exe uninstall` (Admin), danach ggf. die eigenen Installer der abgeloesten Tasks.


## Performance (2026-09-11)

Der Agent selbst kostet ~0,3 % eines Kerns. Teuer war die GPU-Messung: der Heartbeat startete alle 2 s `nvidia-smi.exe`
(25 ms CPU, 60 ms Wall je Start, 43 200 Prozessstarts am Tag, ~1,2 % eines Kerns dauerhaft plus Prozess-Churn). Seit 0.3.0
misst der Agent per **NVML** direkt aus `nvml.dll` (LazyDLL, kein cgo): 0,03 ms je Messung, gleiche Werte wie
`nvidia-smi --query-gpu` (Auslastung %, Speicher MiB). nvidia-smi bleibt als Rueckfall, wenn die DLL fehlt oder NVML dreimal in
Folge scheitert. Pruefen ohne Dienst: `skirnir-agent.exe gpu` zeigt Quelle, Werte und Dauer je Messung. Zweitens hat der
Tunnel-HTTP-Client zu Ollama jetzt einen eigenen Transport (32 Leerlauf-Verbindungen je Host statt der 2 des Go-Defaults), damit
parallele Streams keine TCP-Verbindungen auf- und abbauen. Update wie gewohnt: `Install-Service.ps1` als Admin (stop, kopieren, start).
