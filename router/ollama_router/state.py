"""Der gemeinsame Laufzeitzustand des Routers. Immer als `state.X` ansprechen - CFG, SESSION, REG und
HA_PUB werden beim Start neu gebunden, ein `from .state import CFG` saehe den alten Wert."""

import time


# Der Agent weist sich mit einem Ed25519-Schluessel aus (Signatur ueber eine Zufallszahl). Unbekannte Schluessel landen
# als "wartet auf Freigabe" im Register (nodes.json) und werden in der UI freigegeben; dabei werden Policy (WOL,
# Gewicht, MQTT) gesetzt. Freigegebene Knoten bekommen ihr Konfigurationspaket (MQTT-Zugang, Intervalle) durch den Tunnel.
REG = None
PENDING = {}    # fp -> Tunnel, verbunden aber nicht freigegeben
HA_PUB = None


CFG = None
NODES = {}
MQTT_DIRTY = []   # nicht-leer = sofort publizieren
MQTT_EVENTS = []  # HA-Ereignisse, die ha.py beim naechsten Durchlauf einmal verschickt (nicht retained)
KUERZUNGEN = {"anzahl": 0, "letzte": None}   # bestaetigte stille Kuerzungen seit Routerstart (kontextpruefung.py)
MEASURING = {}    # model -> {"node", "step", "started", "error"} waehrend einer laufenden Messung
PERF = {}         # "model@node" -> Leistungsdaten (passiv aus echten Anfragen + aktiver Benchmark), persistiert in perf.json
PERF_DIRTY = [0.0]


BENCHING = {}     # model -> {"node","step","started","error"}
SESSION = None
DECISIONS = []   # Ringpuffer der letzten Routing-Entscheidungen und Ereignisse (DECISIONS_KEEP Eintraege, ~300 B je Eintrag)
DECISIONS_KEEP = 2000   # Betreiber 2026-09-25: 50 in der UI waren zu wenig, eine Nacht (Guard-Zustandsfolge) war schon weg
DECISIONS_FILE = None   # events.jsonl neben config.yaml (decisions_load setzt ihn; None = keine Persistenz)
_DEC_APPENDED = 0       # Zeilen seit dem letzten Eindampfen
_DEC_WARNED = 0.0       # letzte Warnung 'schreiben fehlgeschlagen' (hoechstens alle 10 min)
CAPS = {}        # model -> capabilities (aus /api/show)
SESSIONS = {}    # routing.session_id -> {node, model, t} (Affinitaet, Stufe 1)
CLOUD = {}       # Stufe 5: provider -> cloud.CloudTarget
METRICS = {"counters": {}, "hist": {}}   # Stufe 4: Prometheus-Zaehler/Histogramme (metrics.py); Gauges kommen live
USAGE = {"days": {}}                     # Stufe 4: Nutzung je Tag/Client/Rolle/Modell, persistiert in usage.json
USAGE_DIRTY = [0.0]
# Client-Authentifizierung (Stufe 2): Laufzeit-Ueberschreibung des Modus (POST /admin/client_auth, gilt bis zum Neustart;
# dauerhaft ist config.yaml) und Zaehler fuer die Beobachtungsphase - wer kommt mit welcher Identitaet, wer ohne.
CLIENT_AUTH_MODE = None
CLIENT_STATS = {"clients": {}, "unauth": {}}
# Eigene Identitaet des Routers fuer den Selbstaufruf der UI ("Ausprobieren", admin.handle_try): Zufall beim Start, nur im
# Speicher, gilt nur von localhost. Ohne das lief der Knopf im enforce-Modus auf 401 (Betreiber, 2026-09-10). Name: skirnir-ui.
INTERNAL_TOKEN = None
INTERNAL_CLIENT = "skirnir-ui"


def decisions_path():
    import os
    return os.path.join(os.path.dirname(os.path.abspath(CFG.path)), "events.jsonl") if CFG is not None and getattr(CFG, "path", None) else None


def decisions_load():
    """Beim Start: die letzten DECISIONS_KEEP Eintraege aus events.jsonl in den Ring uebernehmen und die Datei darauf
    eindampfen. Damit ueberlebt das Protokoll Neustarts und Deploys (Betreiber 2026-09-25: die Nacht war nach dem Deploy weg)."""
    global DECISIONS_FILE, _DEC_APPENDED
    import json
    from .common import log
    DECISIONS_FILE = decisions_path()
    if not DECISIONS_FILE:
        return 0
    rows = []
    try:
        with open(DECISIONS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue   # angerissene Zeile (Absturz beim Schreiben): ueberspringen
                if isinstance(r, dict) and r.get("event") and r.get("t"):
                    rows.append(r)
    except FileNotFoundError:
        return 0
    except Exception as e:  # noqa: BLE001
        log.warning("events.jsonl unlesbar: %s", e)
        return 0
    rows = rows[-DECISIONS_KEEP:]
    DECISIONS[:0] = rows
    del DECISIONS[:-DECISIONS_KEEP]
    _decisions_compact()
    return len(rows)


def _decisions_compact():
    """Datei neu schreiben mit dem, was im Ring steht (atomar ueber .tmp)."""
    global _DEC_APPENDED
    import json
    import os
    if not DECISIONS_FILE:
        return
    tmp = DECISIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in DECISIONS:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, DECISIONS_FILE)
    _DEC_APPENDED = 0


def decisions_append(entry):
    """Eine Zeile anhaengen; nach 4 x DECISIONS_KEEP Zeilen eindampfen, damit die Datei nicht unbegrenzt waechst
    (~300 B je Eintrag: hoechstens ~2,5 MB). Schreibfehler werden gedrosselt gemeldet, das Protokoll im Speicher lebt weiter."""
    global _DEC_APPENDED, _DEC_WARNED
    if not DECISIONS_FILE:
        return
    import json
    try:
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _DEC_APPENDED += 1
        if _DEC_APPENDED >= 4 * DECISIONS_KEEP:
            _decisions_compact()
    except Exception as e:  # noqa: BLE001
        if time.time() - _DEC_WARNED > 600:
            _DEC_WARNED = time.time()
            from .common import log
            log.warning("events.jsonl schreiben: %s", e)


def remember(entry):
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry["t"] = time.time()
    if entry.get("event") in ("busy", "free", "wol", "no_node", "kontext_gekuerzt"):
        MQTT_DIRTY.append(True)
    DECISIONS.append(entry)
    del DECISIONS[:-DECISIONS_KEEP]
    decisions_append(entry)
    try:
        from . import metrics   # spaet, um Importzyklen zu vermeiden
        metrics.count_event(entry)
    except Exception:  # noqa: BLE001
        pass
