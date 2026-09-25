# GPU-Schutz (12V-2x6) – Entwurf v0.1 (2026-09-24)

> **Stand 2026-09-24, abends: umgesetzt** in Agent 0.7.0 (`agent-go/guard.go`, Tests `guard_test.go`) und Router 0.1.2
> (`modes.gpu_guard`, Policy `gpu_guard` je Knoten, HA-Probleme, Metriken, UI-Badge). Entscheide des Betreibers: Default
> 80 %, die Abwahl zählt als HA-Problem (abweichend von Abschnitt 4), `require_fresh_status` Standard aus. Konfigschlüssel
> im Agenten heissen englisch und flach (`power_limit_pct`, `high_load_s`, `stage2_pct`, `recovery_s`, `voltage_warn_v`, …,
> siehe `agent-go/README.md`). Lasttest (5a) und Nachtlauf Stufe 2 (5b) am 2026-09-25 gemessen; offen ist die Messwoche (HA-Historie). Der Router-Teil zu `routing.reason`/`skipped` ist nicht gebaut (Deckel wirkt über `max_inflight`).

Anlass: Berichte über verschmorte 12VHPWR/12V-2x6-Stecker an RTX-5090-Karten nach längerer Zeit unter hohem Strom.
Dieses Dokument prüft, was Router und Agent dagegen tun können, und schlägt einen Schutz vor, der **standardmässig an**
ist und ausdrücklich abgewählt werden muss. Bezug: `agent-go/` (`gpu.go`, `gpu_nvml_windows.go`, `heartbeat.go`,
`mqtt.go`, `config.go`, `provision.go`, `tunnel.go`) und `router/ollama_router/` (`poll.py`, `admission.py`,
`nodes.py`, `registry.py`, `config.py`, `scheduler.py`, `request.py`, `ha.py`). Nur Analyse, nichts davon ist gebaut.

Stand der Quellen: Der Agent las bis 0.5.3 per NVML nur Auslastung und VRAM (`nvmlDeviceGetUtilizationRates`,
`nvmlDeviceGetMemoryInfo[_v2]`), Heartbeat-Intervall vom Router provisioniert (`heartbeat_interval_s`, im Betrieb 2 s,
lokaler Rückfall 3 s). Die Messgrössen sind seit Agent 0.6.0 (gleicher Tag) im Repo (`agent-go/sensors.go`, Heartbeat-Block `sensors`, Router `nodes.<n>.sensors`); die Schwellen unten sind noch unbelegte Vorschläge und entsprechend markiert.

## 1. Bedrohungsmodell – und was Software kann

Der 12V-2x6-Stecker ist für 600 W spezifiziert (6 Adern 12 V, rechnerisch ~8,3 A je Pin bei 600 W, Nennwert ~9,5 A je
Pin). Die Founders Edition und viele Partnerkarten messen den Strom mit **einem** Shunt für alle Pins und verteilen ihn
nicht; bei einem schlechten Kontakt konzentriert sich der Strom auf wenige Adern (Berichte: 20 A+ auf einer Ader) und
der Übergang erhitzt sich, bis Kunststoff schmilzt. Bei 575 W TGP hat die Karte ~4 % Reserve zur Steckerspezifikation.

Ehrlich gesagt kann Software das Grundproblem nicht lösen:
- Sie **sieht keine Einzelpin-Ströme** (kein Sensor auf FE/den meisten Partnerkarten) und erkennt einen defekten
  Kontakt nicht direkt. Ein frischer Kontaktfehler kann in Minuten schaden, Software reagiert in Sekunden bis Minuten
  auf Sekundäreffekte.
- Sie **kann** den Gesamtstrom senken (Power-Limit: weniger Watt = weniger Ampere auf allen Adern, auch auf der
  schlechten), die **Dauer** hoher Last begrenzen und **früh warnen**, wenn Spannungsabfall oder Temperaturen
  von der Norm abweichen.

Der Schutz hier ist also eine Versicherungsprämie: ein paar Prozent Rechenleistung gegen weniger Strom im Stecker und
gegen unbegrenzte Vollast-Dauer. Er ersetzt weder das korrekte Einstecken (Klick, kein Knick am Stecker) noch einen
Kabelwechsel, wenn der Spannungsabfall auffällig ist.

## 2. Messgrössen und ihre Aussagekraft

| Grösse | Quelle | Aussage | Grenzen |
|---|---|---|---|
| Board Power (W) | NVML `nvmlDeviceGetPowerUsage` | Gesamtaufnahme der Karte (Stecker + PCIe-Slot, Slot bis 75 W) | ohne Anmeldung, immer da; überschätzt den Steckerstrom um den Slot-Anteil, also **konservativ** |
| 16-Pin Power (W) | GPU-Z Shared Memory | die Grösse, die zählt: Leistung durch den Stecker | nur wenn GPU-Z läuft (Windows, Benutzersitzung) |
| 16-Pin Spannung (V) | GPU-Z | **einziger Kontaktindikator**: fällt unter Last stärker als üblich ab -> höherer Übergangswiderstand in Kabel/Stecker | Absolutwert hängt vom Netzteil ab (ATX: 12 V ±5 %); darum Abfall gegen die eigene Leerlauf-Referenz messen, nicht nur Absolutwert |
| Power-Limit aktuell/Standard (W) | NVML `GetPowerManagementLimit`, `GetPowerManagementDefaultLimit`, `GetPowerManagementLimitConstraints` | ob der Hebel gesetzt ist und wie weit er darf (Min..Max) | Limit ist Zielwert, nicht Messwert |
| Throttle-Gründe (Bitmaske) | NVML `GetCurrentClocksThrottleReasons` | `SwPowerCap` = das Limit greift (Bestätigung, dass der Hebel wirkt); `HwSlowdown`/`HwPowerBrake` = Hardware bremst selbst -> etwas ist ernsthaft falsch | Bit-Semantik treiberabhängig |
| GPU-Temperatur (°C) | NVML `GetTemperature` | Chip; für den Stecker nur indirekt relevant | – |
| Hot Spot, Speichertemperatur (°C) | GPU-Z | Speicher ist bei LLM-Dauerlast der erste, der leidet | nur mit GPU-Z |
| Lüfter (%) | NVML | Plausibilität (Last ohne Lüfter = Sensorfehler oder Lüfterausfall) | – |

Messbeispiel im Leerlauf (Betreiber): Board 24,5 W, 16-Pin 21,9 W bei 12,1 V. Damit ist die Leerlauf-Referenz der
Spannung ~12,1 V an dieser Karte mit diesem Netzteil.

Schwellen für die Spannung (**Vorschlag, nicht gemessen**): Bei 575 W fliessen ~48 A. Ein gesunder Kabelweg
(Kabel + zwei Steckverbindungen, Grössenordnung wenige mΩ) verliert dabei etwa 0,1–0,3 V. Vorschlag:
Warnung, wenn unter Last (16-Pin >= 300 W) die Spannung **unter 11,6 V** fällt **oder** der Abfall gegen die
Leerlauf-Referenz **mehr als 0,35 V** beträgt; beides erst nach 30 s Bestand. Die Referenz lernt der Agent selbst
(Median der 16-Pin-Spannung bei < 60 W über 24 h, analog zur VRAM-Baseline in `nodes.learn_baseline`). Die Zahlen
sind vor dem Scharfstellen eine Woche lang nur zu protokollieren (Abschnitt 5).

Ohne GPU-Z (Agent als Dienst ohne Anmeldung, Linux, GPU-Z nicht gestartet) bleiben: Board Power, Limits, GPU-Temperatur,
Throttle-Gründe, Lüfter. Das reicht für den Kern des Schutzes (Limit + Dauer), nicht für die Kontaktdiagnose.
**Annahme, zu prüfen**: Ob der Dienst in Sitzung 0 die von GPU-Z in der Benutzersitzung angelegte Shared Memory
überhaupt öffnen kann (Namensraum `Local\` vs `Global\`), ist offen. Falls nicht, braucht GPU-Z einen Start unter
demselben Konto oder der Wert kommt aus einem kleinen Benutzer-Helfer.

Zur Erwartung, wo Hochlast auftritt (**Annahme**): Die Decode-Phase eines LLM ist speicherbandbreitenbegrenzt und
erreicht das Limit meist nicht; Prefill langer Kontexte (64k) und parallele Anfragen (`OLLAMA_NUM_PARALLEL` 2,
`max_inflight` 2) treiben die Karte ans Limit. Der Schutz wirkt also vor allem bei grossen Kontexten und Parallelität.

## 3. Mechanismus-Optionen

### (a) Agent setzt ein Power-Limit

`nvmlDeviceSetPowerManagementLimit` (Bereich aus `GetPowerManagementLimitConstraints`, entspricht `nvidia-smi -pl`).
Das ist der **einzige Hebel, der den Strom tatsächlich senkt**. Zwei Spielarten:
- **statisch**: dauerhaft z. B. 80 % des Standardlimits (5090: 575 -> 460 W; ~38 A statt ~48 A, ~6,4 A je Pin statt 8,0).
  Kosten (**Annahme, zu messen**): wenige Prozent Tempo bei 450–500 W statt 575 W.
- **dynamisch**: normal Standardlimit, nach anhaltender Hochlast über X Minuten tiefer, danach zurück. Spart Tempo
  im Alltag, schützt aber genau in der Anfangsphase eines Dauerlaufs nicht – und das Problem ist Dauerstrom.

Eigenschaften: Braucht Administratorrechte. Der Dienst läuft je nach `install.account` als LocalSystem (Standard in
`config.go`) oder als virtuelles Dienstkonto `NT SERVICE\…` (Vorlage `config.example.yaml`); **ob das virtuelle Konto
das Setzen darf, ist zu prüfen** (NVML antwortet sonst `NVML_ERROR_NO_PERMISSION`). Das Limit ist **flüchtig**: nach
Treiber-Neustart, Reboot oder Treiber-Reset steht wieder der Standard. Der Agent muss es beim Start setzen und bei jedem
Heartbeat prüfen (`GetPowerManagementLimit` != gewünscht -> neu setzen, Ereignis protokollieren). Unter Linux ist NVML im
Agenten nicht angebunden (`gpu_nvml_other.go`), dort gibt es den Hebel vorerst nicht.

### (b) Router drosselt

Der Router kennt je Knoten `inflight`, `effective_max_inflight()` (Policy `max_inflight`, sonst
`modes.admission.max_inflight_default`) und den Zustandsautomaten `offline/free/busy` (`poll.evaluate`). Möglich:
Inflight-Deckel 1 bei Hochlast, Admission-Pause (neue Anfragen warten in `admission.WAITING`), Bevorzugung anderer
Knoten im Score. **Nicht** geeignet: den Knoten `busy` setzen – `busy` bedeutet "fremd belegt", entlädt grosse Modelle
(`unload_on_busy`) und lässt nur `busy_ok`-Stufen zu. Ein kleines Modell zieht auf derselben Karte nicht weniger Strom
pro Sekunde, und das Entladen kostet danach einen Kaltstart. Drosselung senkt Parallelität und begrenzt Dauer, aber
**nicht die Spitzenleistung** einer einzelnen Anfrage; als alleiniger Schutz ist (b) schwach.

### (c) Nur warnen

HA-Problem (`binary_sensor … problem`, Attribut `problems` in `ha.ha_snapshot`), MQTT-Ereignis am Agent-Gerät, Log.
Kostet nichts, schützt nichts – aber ohne (c) merkt niemand, dass (a) verweigert wurde oder die Spannung wegsackt.

### Abwägung und Default

| | senkt Strom | begrenzt Dauer | warnt | Kosten | Voraussetzung |
|---|---|---|---|---|---|
| (a) statisch | **ja** | nein | – | wenige % Tempo (Annahme) | Admin, NVML, Windows |
| (a) dynamisch | verzögert | ja | – | ~0 im Alltag | wie oben |
| (b) | nein | ja | – | Wartezeit bei Parallelität | Agent meldet Zustand |
| (c) | nein | nein | ja | 0 | – |

**Default: (a) statisch + (a) dynamisch als zweite Stufe + (b) + (c).** Begründung: Nur (a) wirkt physikalisch;
statisch, weil der Schaden von Dauerstrom kommt und die erste Stunde eines Nachtlaufs nicht ungeschützt sein soll.
Die dynamische Stufe und (b) fangen Dauer-Vollast am (bereits gesenkten) Limit ab. (c) macht Ausfälle des Schutzes
sichtbar. Wer die Prozente braucht, wählt ab – und sieht das in HA und in der Router-UI.

Rückfallordnung, wenn etwas fehlt:
- **NVML-Setzen verweigert** (Rechte, Linux, Fremdkarte): (a) entfällt, Agent meldet einmalig `limit_verweigert`
  (HA-Problem "GPU-Schutz: Power-Limit nicht setzbar"), (b) + (c) laufen weiter. Der Knoten bleibt nutzbar.
- **GPU-Z fehlt**: keine Spannungs-/Speicherwarnung, Hochlast wird an der **Board** Power gemessen (konservativer).
  Keine Fehlermeldung, nur Attribut `quelle: nvml` statt `nvml+gpuz`.
- **Kein Agent-Heartbeat**: der Router sieht keinen Zustand; heute schon Problem "kein Agent-Heartbeat" (`agent_missing_s`).
  Zusätzlich nichts, aber der Router setzt einen Knoten ohne frischen Schutzstatus nicht auf Inflight > 1, wenn dessen
  Policy `gpu_guard.enabled` ist (fail-safe, **Vorschlag**; kostet bei Agent-Ausfall Parallelität).
- **Fremdeingriff** (Afterburner o. ä. setzt das Limit anders): Der Agent **senkt nur, erhöht nie**. Steht das Limit
  tiefer als gewünscht, lässt er es. Steht es höher, setzt er nach, höchstens einmal je `reapply_s`, und meldet nach
  drei Konflikten in Folge `fremdeingriff` als Problem statt endlos zu ringen.

## 4. Vorschlag

### Konfiguration

Der Agent ist der Ort des Hebels, der Router der Ort der Policy je Knoten. Beides ist einzeln abwählbar; die
Abwahl ist ein eigener Wert, kein fehlender Schlüssel (Standard = an, wie `tunnel.enabled` in `config.go`).

Agent, `config.yaml` (lokal), Block `gpu_guard:`; der Router kann dieselben Werte per `Provision` (`provision.go`,
neues Feld `gpu_guard`) nachschieben, lokale Angaben gewinnen wie bei `mqtt:`:
```yaml
gpu_guard:
  enabled: true              # false = ausdrücklich abgewählt; wird geloggt, in /health und an HA gemeldet
  power_limit_pct: 80        # Dauerlimit in % des Standardlimits (5090: 460 W); 100 = kein Dauerlimit. Geklemmt auf [Min, Standard]
  # power_limit_w: 450       # alternativ absolut, gewinnt vor pct; nie über dem Standardlimit
  reapply_s: 30              # Limit prüfen und bei Abweichung neu setzen (flüchtig nach Treiber-Neustart)
  hochlast:
    ab_pct: 90               # Hochlast = 16-Pin- (sonst Board-) Leistung >= 90 % des aktiven Limits ...
    dauer_s: 600             # ... ununterbrochen so lange -> Stufe 2
    stufe2_pct: 70           # zweites, tieferes Limit während der Erholung (5090: ~400 W)
    erholung_s: 300          # so lange bleibt Stufe 2, danach zurück auf das Dauerlimit
  spannung:                  # nur mit GPU-Z; Vorschlag, erst nach Messwoche scharf
    warn_unter_v: 11.6
    warn_abfall_v: 0.35
    bestand_s: 30
  temperatur:
    speicher_warn_c: 95      # Annahme (GDDR7-Herstellerangaben prüfen)
    hotspot_warn_c: 100
```
Begründung der Defaults: 80 % nimmt der Karte ~10 A ab, bleibt aber oberhalb des Bereichs, in dem Ollama messbar
langsamer wird (**Annahme**, Messung in Abschnitt 5). 600 s Hochlast am Limit entspricht einer langen
Batch-/Nachtlauf-Phase, nicht einer Sprachanfrage. 70 % Stufe 2 ist der Wert, der die Karte spürbar kühler laufen
lässt, ohne die Anfrage abzubrechen. `reapply_s` 30 ist ein Kompromiss zwischen "Reboot bleibt unbemerkt" und
Konfliktrate mit Fremdtools.

Router, Policy je Knoten im Register (`registry.py`, neben `max_inflight`, `busy_gpu_util_pct`, `busy_foreign_gib`;
UI `Registrierte Agenten`), Schema in `config.py` unter `nodes.*` und `modes.gpu_guard`:
```yaml
modes:
  gpu_guard:
    enabled: true            # Router reagiert auf den Schutzstatus aus dem Heartbeat (Inflight-Deckel, Score, HA)
    throttled_max_inflight: 1
    score_penalty: 40        # wie ein halb voller Knoten (modes.score: voller Knoten -50)
    require_fresh_status: true   # ohne frischen Status kein Inflight > 1 (Vorschlag, s. Rückfallordnung)
nodes:
  <name>:
    gpu_guard: true          # Policy je Knoten; false = Router ignoriert den Status dieses Knotens (RTX 4080 mit 8-Pin z. B.)
```

### Zustandsfolge im Agenten

```
aus          <- gpu_guard.enabled false           (meldet "aus", setzt nichts, erhöht nichts)
unverfügbar  <- NVML fehlt / Setzen verweigert / Linux (meldet Grund; Messwerte trotzdem, wenn lesbar)
normal       <- Limit = Dauerlimit gesetzt und bestätigt (GetPowerManagementLimit == Ziel)
hochlast     <- Leistung >= ab_pct des aktiven Limits seit dauer_s/2   (Vorwarnung, noch keine Aktion)
gedrosselt   <- Leistung >= ab_pct ununterbrochen seit dauer_s        -> Limit = stufe2_pct, Ereignis, Router-Hinweis
erholung     <- seit erholung_s in gedrosselt                          -> Limit = Dauerlimit, noch 60 s "erholung" melden
normal       <- Erholung abgelaufen
jeder Zustand: Limit != erwartet -> neu setzen (nur senken) + Ereignis limit_gesetzt | fremdeingriff | limit_verweigert
```
Die Zustandsermittlung ist eine reine Funktion über die letzten Messwerte (wie `kuerzung.go`), damit sie ohne Karte
testbar ist. Der Heartbeat (`heartbeat.payload`) trägt zusätzlich:
```json
"gpu_guard": {"state": "normal", "quelle": "nvml+gpuz", "power_w": 412.0, "pin16_w": 388.5, "pin16_v": 11.93,
              "limit_w": 460, "default_limit_w": 575, "throttle": ["sw_power_cap"], "hochlast_s": 0}
```
`HBACK` (`tunnel.dispatch`) bleibt `{state, busy_reason}`, ergänzt um `gpu_guard_ack: true|false` (Router-Policy
an/aus), damit `/health` des Agenten zeigt, ob der Router mitspielt.

### Router-Verhalten

- `poll.apply_heartbeat` übernimmt `gpu_guard` in den Knoten (`node.guard`, `node.guard_ts`); kein neuer Zustand im
  Automaten `offline/free/busy`, sondern ein Flag neben dem Zustand – so bleibt `busy` (fremde Nutzung) unberührt.
- `nodes.effective_max_inflight()`: bei `guard.state == gedrosselt` -> `min(bisher, throttled_max_inflight)`; die
  Admission (`admission.saturated`) wartet dann wie heute mit Priorität statt Ollamas Schlange zu füllen.
- `scheduler`: Score-Abzug `score_penalty` bei `hochlast|gedrosselt`, damit ein zweiter Knoten (RTX 4080) die
  Anfrage bekommt, wenn er die Stufe tragen kann. Kein harter Ausschluss: eine Sprachanfrage soll nicht auf 503 laufen,
  weil die einzige 5090 gerade gedrosselt ist.
- `routing.reason` bleibt `rank|warm-first|…`; neu im Eintrag `route`: `"guard": {"node": "<name>", "state": "gedrosselt"}`
  und in `skipped` der Grund `"gpu_guard: <knoten> gedrosselt, Deckel 1"` wenn ein Knoten deshalb überlaufen wurde.
- Ereignisse über `state.remember`: `{"event": "gpu_guard", "node", "state", "limit_w", "reason"}`; `gpu_guard` in die
  Liste der `MQTT_DIRTY`-Ereignisse aufnehmen (sofortige HA-Aktualisierung) und in `metrics.count_event` mitgezählt
  (`skirnir_events_total{event="gpu_guard",state=…}`). Gauges `skirnir_node_gpu_power_w`, `_limit_w`, `_pin16_v`.
- Audit: `limit_gesetzt`, `limit_verweigert`, `fremdeingriff`, `abgewählt` sind sicherheitsrelevante Betriebsereignisse
  und gehen zusätzlich ins Audit-Log (`auth.audit`, `client_auth.audit_log`), damit sie nach einem Vorfall nachlesbar sind.

### HA-Entitäten

Agent-Gerät (`mqtt.go`, `sensorDefs`, Gruppe `gpu_guard` = nur wenn NVML da; Untergruppe `gpuz` für Spannung/Speicher):
- `sensor gpu_power_w`, `gpu_pin16_power_w`, `gpu_pin16_voltage_v`, `gpu_power_limit_w`, `gpu_power_limit_default_w`,
  `gpu_memory_temp_c`, `gpu_hotspot_c` (`state_class: measurement`, `device_class: power|voltage|temperature`)
- `sensor gpu_guard_state` (aus|unverfügbar|normal|hochlast|gedrosselt|erholung) mit Attributen `quelle`, `throttle`,
  `hochlast_s`, `grund`
- `binary_sensor gpu_guard_problem` (`device_class: problem`): ON bei `unverfügbar` (mit Grund), `fremdeingriff`,
  Spannungs- oder Temperaturwarnung. **Nicht** ON bei `aus` – die Abwahl ist eine Entscheidung, kein Fehler; sie
  steht im Zustandssensor.
- `event gpu_guard` (Topic `<device>/gpu_guard`, nicht retained) mit `event_types` `hochlast`, `gedrosselt`, `erholung`,
  `limit_gesetzt`, `limit_verweigert`, `fremdeingriff`, `spannung_niedrig`, `temperatur_hoch`.

Router-Gerät (`ha.py`): `problems` erhält `"Knoten <name>: GPU-Schutz nicht setzbar (<grund>)"` und
`"Knoten <name>: 16-Pin-Spannung <v> V unter Last"`; `nodes.<name>` im Zustandsbild bekommt `gpu_guard` (Zustand,
Limit) für die UI-Karte. Ein Knoten mit `gpu_guard: aus` zeigt das in der Knotenkarte der Web-UI als Badge.

Agent-Log, Info-Stufe: `gpu-guard: Limit 575 -> 460 W gesetzt (80 % Standard)`, `… gedrosselt -> 402 W für 300 s`;
Warnstufe: `… Limit stand auf 575 W (Fremdeingriff?), erneut 460 W`, `… Setzen verweigert (NVML rc=4) – nur Beobachtung`.

## 5. Testplan – ohne die Karte zu gefährden

Regel für jeden Test mit echter Hardware: Der Guard **klemmt jedes Ziel auf [Min-Limit, Standardlimit]**; ein Test
darf das Limit nie über den Standard heben. Testreihenfolge vom Ungefährlichen zum Realen:

1. **Agent, reine Funktion** (`gpu_guard_test.go`, Muster `kuerzung_test.go`): synthetische Messreihen ->
   erwartete Zustandsfolge; Hochlast knapp unter `ab_pct` bleibt `normal`; Unterbrechung setzt den Zähler zurück;
   Erholung endet nach `erholung_s`; Spannungswarnung erst nach `bestand_s`; Referenzlernen ignoriert Lastphasen.
2. **Agent, NVML-Attrappe**: Interface über `nvmlDev` mit Stub, der Set-Aufrufe protokolliert und wahlweise rc=4
   liefert -> `unverfügbar` mit Grund, kein zweiter Versuch je `reapply_s`, Ereignis einmalig. Stub meldet nach
   "Reboot" wieder 575 W -> genau ein Set-Aufruf. Stub meldet 400 W (tiefer als Ziel) -> kein Set-Aufruf (nie erhöhen).
3. **Agent, Trockenlauf am echten Rechner**: Verb `gpu --guard-dry-run` (Erweiterung von `main.go` Verb `gpu`): liest
   Limits, Constraints, Leistung, Throttle-Gründe, GPU-Z-Werte, zeigt, was gesetzt **würde**, setzt nichts. Damit
   auch die Sitzungs-0-Frage zur Shared Memory klären (als Dienst gestartet).
4. **Router, Fake-Knoten** (`test/run_tests.py`, `fake_agent.py`, `fake_ollama.py`): `hb()` bzw. der HB-Rahmen des
   Fake-Agenten mit `gpu_guard`-Block; Prüfungen: `gedrosselt` -> `max_inflight` 1 im `/admin/state`, zweite Anfrage
   wartet in `admission.view()`, `skipped`-Grund und `guard` im `route`-Eintrag, Ereignis `gpu_guard` in `decisions`,
   HA-Snapshot mit Problem bei `unverfügbar` und **ohne** Problem bei `aus`, Policy `gpu_guard: false` je Knoten
   ignoriert den Status (Deckel bleibt 2), veralteter Status (`require_fresh_status`) begrenzt auf 1.
   `fake_ollama.py` hat `sleep_s` für lange Anfragen, das reicht für die Parallelitätstests.
5. **Messwoche, nur beobachten** (`enabled: true`, `power_limit_pct: 100`, keine Aktion): Verteilung von Board/16-Pin
   Power, Spannung im Leerlauf und unter Last, Throttle-Gründe, Häufigkeit von "Hochlast >= 90 %" im echten Betrieb.
   Ergebnis entscheidet, ob 80/90/600 passen und ob 11,6 V / 0,35 V sinnvolle Schwellen sind.
6. **Lasttest mit Limit** (bestehender Benchmark `perf.py`, Rollen `standard`/`gross`, 8k und 64k Kontext):
   je Limit 575/500/460/400 W tok/s, TTFT und 16-Pin-Spannung messen. Belegt oder widerlegt die "wenige Prozent"-Annahme
   und liefert den Wert, ab dem Stufe 2 spürbar wird. Vorher `verify-the-instrument`: Throttle-Bit `SwPowerCap` muss
   unter Last gesetzt sein, sonst greift das Limit nicht und die Messung misst nichts.
7. **Reboot-Probe**: Knoten neu starten, nach dem Dienststart im Log `Limit … gesetzt` und in HA `gpu_power_limit_w`
   = Ziel innerhalb von `reapply_s` + Startzeit prüfen.

## 5a. Messergebnisse 2026-09-25 (Test 6, Lasttest mit Limit)

Aufbau: `tools/guard_loadtest.py` (erhöht, `nvidia-smi -pl` je Stufe, Guard auf 100 % geparkt), je Stufe zwei Prefill-Anfragen
mit ~18k Token frischem Text, zwei parallele Generierungen (~700 Token), dann Prefill und Generierung parallel; dazu
`/health` des Agenten jede Sekunde (Board Power NVML, 16-Pin GPU-Z, Throttle-Bits). RTX 5090, Treiber 616.56, Ollama 0.11,
Leerlauf-Referenz 12,05 V. Die Stufe 500 W nur beim MoE-Modell.

| Modell | Limit W | Prefill tok/s | Gen tok/s (2 parallel) | Prefill neben Gen | Board max W | 16-Pin max W | 16-Pin unter Last V | `sw_power_cap` Proben |
|---|---|---|---|---|---|---|---|---|
| qwen3.6:35b-a3b (MoE, 3B aktiv) | 575 | 6535 | 179 | 6536 | 322 | 329 | 11,94 | 0/28 |
| | 500 | 6380 | 178 | 6504 | 333 | 324 | 11,93 | 0/25 |
| | 460 | 6160 | 178 | 6312 | 310 | 298 | – (<300 W) | 2/26 |
| | 400 | 6052 | 170 | 6172 | 304 | 299 | – | 8/25 |
| gemma4:26b (dicht) | 575 | 12794 | 190 | 10735 | 469 | **543** | 11,88 (min 11,81) | 4/16 |
| | 460 | 12655 | 186 | 9646 | 446 | 459 | 11,87 | 4/15 |
| | 400 | 12218 | 183 | 9440 | 346 | 430 | 11,88 | 6/16 |

Befunde:
- **Das MoE-Modell erreicht das Limit nie**: 330 W Spitze bei 575 W erlaubt. Für die Rollen auf qwen3.6:35b-a3b (die meisten)
  ist das 80-%-Limit im Alltag wirkungslos und kostenlos; erst bei 400 W greift `sw_power_cap` gelegentlich (8 von 25 Proben),
  Tempo –7 % Prefill, –5 % Generierung.
- **Das dichte Modell bringt die Karte ans Limit**: 543 W am 16-Pin bei 575 W (Board-Mittel 469 W). Genau dieser Fall ist
  der Grund für den Schutz. Bei 460 W kostet es **–1 % Prefill, –2 % Generierung, –10 % Prefill neben laufender
  Generierung**; bei 400 W –4 % / –4 % / –12 %. Die Annahme „wenige Prozent“ hält für Einzelanfragen, unter Parallelität
  ist es ein Zehntel.
- **Spannung**: Abfall gegen die Leerlauf-Referenz 0,17 V bei 330 W, **0,24 V bei 543 W** (min 11,81 V). Kabelweg gesund;
  die Warnschwelle 0,35 V hat ~0,1 V Reserve, 11,6 V absolut ~0,2 V. Beide Schwellen bleiben wie vorgeschlagen.
- **Instrument**: `sw_power_cap` erscheint nur, wenn das Limit wirklich bindet (0 Proben beim MoE bei 575/500 W) – der
  Hebel ist damit als wirksam belegt. Grenzen der Messung: 1-s-Abtastung über den Agenten (2-s-NVML, 1-s-GPU-Z) glättet
  Spitzen; 16-Pin-Werte können über der Board-Power liegen, weil beide Quellen nicht zeitgleich abgetastet werden. Die
  Anfragen dauern 2–9 s – Dauerlast im Sinne der Stufe 2 (600 s) wurde nicht erzeugt, dafür bräuchte es einen Nachtlauf.
- Temperaturen unkritisch: Speicher max 58 °C, Hot Spot max 72 °C.

Entscheidung: Default 80 % bleibt. Stufe 2 (70 %) kostet bei dichten Modellen ~4 %/12 %, ist also als Erholungsstufe
tragbar. Test 5 (Messwoche) läuft passiv über die HA-Historie der Sensoren `GPU Leistung`, `GPU 16-Pin Leistung`,
`GPU 16-Pin Spannung`, `GPU-Schutz` – auszuwerten nach einer Woche Betrieb.

## 5b. Nachtlauf Stufe 2, 2026-09-25 04:13–04:30 (Zustandsfolge unter Dauerlast)

Aufbau: `tools/guard_stage2_run.py` – vier Worker schicken ohne Pause Prefill-Anfragen (~17k Token, gemma4:26b) durch
`/admin/try`; der Router hält zwei davon auf dem Knoten, der Rest wartet in der Admission. `/health` alle 2 s. Guard mit
Standardwerten (80 %, 90 %/600 s, 70 %/300 s), Agent 0.7.1.

| Zeit | Ereignis | Limit | Beleg |
|---|---|---|---|
| 04:13:43 | Start, `normal` | 460 W | Leerlauf 17 W |
| 04:19:08 | `hochlast` nach 301 s | 460 W | Agent-Log „Hochlast seit 302 s (456 W von 460 W)“ |
| 04:24:08 | `gedrosselt` nach 601 s | **403 W** | `nvidia-smi` 403 W; Router: `max_inflight` 1, drei Anfragen wartend, Ereignis `gpu_guard` |
| 04:24:49 | Last beendet | 403 W | |
| 04:29:09 | `erholung` nach 300 s | 460 W | Limit vom Guard selbst angehoben (eigener Wert) |
| 04:30:09 | `normal` nach 60 s | 460 W | |

Unter Last (312 Proben): 16-Pin Median 451 W, Spitzen bis 525 W (das Limit ist ein Mittelwert über das NVML-Fenster,
Momentanwerte liegen darüber), Board Median 397 W, `sw_power_cap` in 69 % der Proben. **16-Pin-Spannung min 11,71 V**,
also 0,32–0,34 V unter der Leerlauf-Referenz (12,03–12,05 V) – knapp unter der Warnschwelle 0,35 V, keine Warnung.
Speicher max 78 °C, Hot Spot max 88,5 °C (Schwellen 95/100 °C). Prefill-Tempo Median 11 473 tok/s.

Befunde:
- **Die Zustandsfolge stimmt** und die Zeiten sind auf die Sekunde die konfigurierten. Die Router-Seite (Deckel 1, Ereignis,
  HA-Attribute) greift im selben Heartbeat.
- **Bug gefunden und behoben (0.7.1):** mit „ununterbrochen“ auf 2-s-Raster kam der Zähler bei echter Batchlast nie über
  ~40 s, weil die Leistung zwischen zwei Anfragen (Warteschlangen-Übergabe, Antwort) für Sekundenbruchteile abfällt. Neu
  `high_load_gap_s` (15 s): Lücken bis dahin zählen nicht als Unterbrechung. Im Lauf kam eine 200-W-Probe bei 337 s vor,
  der Zähler lief weiter.
- **Spannungsabfall wächst mit der Dauer**: 0,24 V bei 543 W kurz (5a), 0,32–0,34 V bei ~450 W über zehn Minuten – der
  Kabelweg wird warm. 0,35 V ist damit an dieser Karte die Grenze zwischen „gesund, warm“ und „auffällig“. Beobachten (HA-
  Historie); löst die Warnung im Alltag ohne erkennbaren Grund aus, ist 0,40 V der nächste Kandidat, nicht die Deaktivierung.
- **Nebenbefund Router**: während des Modellwechsels (qwen3.6 → gemma4:26b, VRAM-Fit) beantwortete der Router ~30 s lang
  alle Anfragen sofort mit 503 `no node available` (778 von 1142), statt sie bis zum Laden zu halten. Für den Guard
  belanglos, für Batch-Clients ein eigenes Thema (Roadmap).

## 6. Offene Fragen und Grenzen

- **Rechte des Dienstkontos**: Darf `NT SERVICE\OllamaRouterAgent` das Limit setzen? Falls nicht, bleibt LocalSystem
  oder ein Helfer mit erhöhten Rechten; beides ist eine Sicherheitsabwägung, die der Betreiber trifft.
- **GPU-Z aus Sitzung 0** (Shared-Memory-Namensraum) – Test 3 klärt es.
- **Persistenz des Limits**: `nvidia-smi -pl` ist flüchtig; ob neuere NVML-Versionen eine persistente Variante bieten,
  ist nicht verifiziert (**Annahme: flüchtig**, darum `reapply_s`).
- **Mehrere GPUs**: Agent und Guard behandeln heute Index 0. Ein Knoten mit zwei Karten braucht den Guard je Karte.
- **Karten ohne 12V-2x6** (RTX 4080 mit 12VHPWR oder 8-Pin-Partnerkarten): Der Guard läuft auch dort (Limit senken schadet
  nie), aber die Steckerbegründung passt nicht; Policy je Knoten `gpu_guard: false` oder `power_limit_pct: 100` ist dort
  legitim. Den Kartentyp aus `facts.gpu` automatisch zu erkennen wäre möglich, aber eine Liste, die veraltet.
- **Was der Guard nicht sieht**: Ein Kontaktfehler, der die Spannung am Kartenanschluss nicht messbar drückt (GPU-Z misst
  hinter dem Stecker, nicht am Pin), bleibt unsichtbar. Der Guard senkt in dem Fall trotzdem den Gesamtstrom – das ist
  sein eigentlicher Wert – aber er kann keinen Alarm geben.
- **Tempo-Kosten** sind bis Test 6 eine Annahme. Fällt der Verlust bei 80 % höher aus als erwartet, ist 85–90 % der
  Kompromiss; unter 70 % Dauerlimit lohnt sich die 5090 gegenüber einer kleineren Karte nicht mehr.
- **Betreiber-Entscheid vor dem Bau**: Default 80 % oder 85 %? Soll `aus` doch als HA-Problem zählen (eine Abwahl, die
  vergessen wurde, ist der wahrscheinlichste Fehlerfall)? Soll der Router einen Knoten ohne Guard-Status wirklich auf
  Inflight 1 begrenzen (Preis: Parallelität bei jedem Agent-Ausfall)?
