# ollama-router-agent (Windows-Dienst)

Eine .exe (Go, statisch) ersetzt auf einem GPU-Rechner die Dauerlauf-Scheduled-Tasks: Heartbeat an den Router,
MQTT-Geraet fuer Home Assistant, Aufsicht ueber lokale KI-Dienste (z. B. einen Wyoming-STT-Server als Kindprozess).

| Datei | Zweck |
|---|---|
| `*.go` | `main` (Verben), `app` (Verdrahtung), `config`, `logging`, `gpu` (NVML direkt aus `nvml.dll`, Rueckfall nvidia-smi; `gpu_nvml_windows.go`), `heartbeat`, `secrets` (Secret-Store, Universal-Auth-API), `mqtt`, `supervisor` (Kinder + Job-Objekt), `health`, `service` (SCM), `install` |
| `build.sh` | Cross-Compile aus WSL: `wsl -e bash -lc '/mnt/c/<workspace>/skirnir/agent-go/build.sh 0.1.1'` → `dist/ollama-router-agent.exe` |
| `config.example.yaml` | Vorlage fuer neue Rechner **mit Installationsanleitung Windows/Linux im Kopf**; die echte Datei liegt unter `C:\ProgramData\ollama-router-agent\config.yaml` |
| `ollama-router-agent.service` | systemd-Unit fuer Linux-Knoten (`run --config /etc/ollama-router-agent/config.yaml`) |
| `Install-Service.ps1` | Umschalten/Update, einmal als Administrator: Binary nach Program Files, alte Tasks weg, `install`, `start` |

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
(`dist/ollama-router-agent-linux-amd64`).

## Tunnel (0.3.0, Normalweg)

`tunnel.enabled: true` (Standard): ausgehende WebSocket-Verbindung zum Router (`/v1/tunnel/<node>`, Router-Token), der
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
ollama-router-agent.exe run --config <pfad>     Konsole (Tests), Ctrl-C beendet
ollama-router-agent.exe status                  Dienstzustand + /health
ollama-router-agent.exe start|stop|restart      ohne UAC (Installer setzt das Steuerrecht fuer <user>)
ollama-router-agent.exe install|uninstall       Admin
ollama-router-agent.exe check-config | mqtt-clear | version
ollama-router-agent.exe gpu                     Messquelle und alle Sensoren einmal ausgeben (auch GPU-Z, falls es laeuft)
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
Mit dem empfohlenen virtuellen Dienstkonto `NT SERVICE\OllamaRouterAgent` ist der Zugriff verweigert; dafuer gibt es das
**Relay**: `Install-Service.ps1` legt die Aufgabe `OllamaRouterAgent-GpuzRelay` an (bei Anmeldung, Gruppe Benutzer, ohne
Adminrechte, versteckt, `conhost --headless`), die `ollama-router-agent.exe gpuz-relay` startet. Das Relay liest den Block
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

- Logs: `C:\ProgramData\ollama-router-agent\logs\agent.log`, `stt.log` und `ollama.log` (rotiert, 10 × 5 MB); Start/Stopp auch im
  Windows-Ereignisprotokoll (Quelle `OllamaRouterAgent`).
- Dienstkonto `NT SERVICE\OllamaRouterAgent`; der Installer gibt ihm Lesen auf die Pfade in `grant_read`, Aendern auf `grant_modify` und
  `ProgramData\ollama-router-agent`, und schottet das Config-Verzeichnis ab
  (SYSTEM, Administratoren, Dienstkonto, <user>).
- **Ollama als Kind des Dienstes:** `ollama.exe serve` laeuft als `children`-Eintrag, nicht
  mehr als Tray-App aus dem Startup-Ordner. Grund: die Tray-App startet erst mit der Anmeldung, und ihr Autostart-Eintrag
  kann deaktiviert sein - dann kommt Ollama nach einem Reboot nie hoch und der Knoten bleibt fuer
  Skirnir offline, obwohl Agent und Tunnel laufen. Was dazu gehoert: Benutzer-Umgebungsvariablen von <user> gelten fuer das
  Dienstkonto nicht, darum stehen `OLLAMA_HOST/MODELS/NUM_PARALLEL/FLASH_ATTENTION/CONTEXT_LENGTH/LOAD_TIMEOUT` als `env`
  am Kind; `OLLAMA_MODELS` zeigt auf den bestehenden Modellspeicher (Dienstkonto `(OI)(CI)M`, `ollama pull` schreibt dort weiter),
  `USERPROFILE` auf `ProgramData\ollama-router-agent\ollama-home` (eigener `~/.ollama`-Schluessel des Dienstes); Lesen
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
- Update: neue Binary bauen, `Install-Service.ps1` als Admin (erkennt den bestehenden Dienst: stop, kopieren, start).
  Auf Knoten ohne Kindprozesse reicht die Binary plus die Config; `grant_read`
  und `firewall` bleiben dort leer.
- Rueckbau: `ollama-router-agent.exe uninstall` (Admin), danach ggf. die eigenen Installer der abgeloesten Tasks.


## Performance (2026-09-11)

Der Agent selbst kostet ~0,3 % eines Kerns. Teuer war die GPU-Messung: der Heartbeat startete alle 2 s `nvidia-smi.exe`
(25 ms CPU, 60 ms Wall je Start, 43 200 Prozessstarts am Tag, ~1,2 % eines Kerns dauerhaft plus Prozess-Churn). Seit 0.3.0
misst der Agent per **NVML** direkt aus `nvml.dll` (LazyDLL, kein cgo): 0,03 ms je Messung, gleiche Werte wie
`nvidia-smi --query-gpu` (Auslastung %, Speicher MiB). nvidia-smi bleibt als Rueckfall, wenn die DLL fehlt oder NVML dreimal in
Folge scheitert. Pruefen ohne Dienst: `ollama-router-agent.exe gpu` zeigt Quelle, Werte und Dauer je Messung. Zweitens hat der
Tunnel-HTTP-Client zu Ollama jetzt einen eigenen Transport (32 Leerlauf-Verbindungen je Host statt der 2 des Go-Defaults), damit
parallele Streams keine TCP-Verbindungen auf- und abbauen. Update wie gewohnt: `Install-Service.ps1` als Admin (stop, kopieren, start).
