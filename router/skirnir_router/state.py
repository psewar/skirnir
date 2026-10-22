"""Der gemeinsame Laufzeitzustand des Routers. Immer als `state.X` ansprechen - CFG, SESSION, REG und
HA_PUB werden beim Start neu gebunden, ein `from .state import CFG` saehe den alten Wert."""

import asyncio
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
TASKS = set()     # Hintergrund-Tasks (spawn): der Loop haelt nur schwache Referenzen, ein unreferenzierter Task kann vor dem Ende eingesammelt werden
MQTT_EVENTS = []  # HA-Ereignisse, die ha.py beim naechsten Durchlauf einmal verschickt (nicht retained)
TRUNCATIONS = {"count": 0, "last": None}   # bestaetigte stille Kuerzungen seit Routerstart (context_check.py)
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
METRICS = {"counters": {}, "hist": {}}   # Stufe 4: Prometheus-Zaehler/Histogramme (metrics.py); Gauges kommen live; persistiert in metrics.json
METRICS_DIRTY = [0.0]                    # Zeitpunkt der ersten ungesicherten Aenderung (tick_loop sichert nach METRICS_SAVE_S)
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


def spawn(coro):
    """asyncio.create_task mit Halter: Referenz in TASKS bis zum Ende (RUF006, Python-Doku zu create_task)."""
    t = asyncio.get_running_loop().create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


def decisions_path():
    import os
    return os.path.join(os.path.dirname(os.path.abspath(CFG.path)), "events.jsonl") if CFG is not None and getattr(CFG, "path", None) else None


def decisions_load():
    """Beim Start: die letzten DECISIONS_KEEP Eintraege aus events.jsonl in den Ring uebernehmen und die Datei darauf
    eindampfen. Damit ueberlebt das Protokoll Neustarts und Deploys (Betreiber 2026-09-25: die Nacht war nach dem Deploy weg)."""
    global DECISIONS_FILE, _DEC_APPENDED
    import os
    from .common import log
    DECISIONS_FILE = decisions_path()
    if not DECISIONS_FILE or not os.path.exists(DECISIONS_FILE):
        return 0
    try:
        with open(DECISIONS_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, ValueError) as e:  # exception-ok: file system and the file's encoding (UnicodeDecodeError)
        log.warning("events.jsonl unlesbar: %s", e)
        return 0
    rows = [r for r in map(_journal_row, lines) if r is not None]
    rows = rows[-DECISIONS_KEEP:]
    DECISIONS[:0] = rows
    del DECISIONS[:-DECISIONS_KEEP]
    _decisions_compact()
    return len(rows)


# Namen in events.jsonl vor Router 0.9.0 (design/english-names.md): beim Laden uebersetzt, damit Filter alte und neue
# Eintraege gleich finden. Schluessel: Feld -> {alt: neu}; dazu die Felder des Kuerzungs-Ereignisses.
JOURNAL_BEFORE_0_9 = {
    "event": {"kontext_gekuerzt": "context_truncated"},
    "reason": {"spiel-leicht": "game_light", "spiel-vorbei": "game_ended", "ausweichen": "fallback", "abgewaehlt": "deselected"},
    "state": {"aus": "off", "unverfuegbar": "unavailable", "hochlast": "high_load", "gedrosselt": "throttled", "erholung": "recovery"},
    "status": {"cloud uebersprungen": "cloud_skipped", "kein Knoten": "no_node"},
}
TRUNCATION_FIELDS_BEFORE_0_9 = {"geschaetzt": "estimated", "angekommen": "received", "quote": "ratio", "sicher": "certain"}


def _journal_english(r):
    """Ein Eintrag aus events.jsonl mit den Namen ab Router 0.9.0."""
    for feld, namen in JOURNAL_BEFORE_0_9.items():
        if r.get(feld) in namen:
            r[feld] = namen[r[feld]]
    if r.get("event") == "context_truncated":
        r = {TRUNCATION_FIELDS_BEFORE_0_9.get(k, k): v for k, v in r.items()}
    return r


def _journal_row(line):
    """Eine Zeile aus events.jsonl als Eintrag (Namen ab 0.9.0), None fuer Leerzeilen und angerissene Zeilen (Absturz beim Schreiben)."""
    import json
    if not line.strip():
        return None
    try:
        r = json.loads(line)
    except ValueError:  # exception-ok: a line torn by a crash while writing; whether it parses shows only the parser
        return None
    return _journal_english(r) if isinstance(r, dict) and r.get("event") and r.get("t") else None


def _decisions_compact():
    """Datei neu schreiben mit dem, was im Ring steht (atomar ueber .tmp)."""
    global _DEC_APPENDED
    import json
    import os
    if not DECISIONS_FILE:
        return
    tmp = DECISIONS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for e in DECISIONS:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, DECISIONS_FILE)
    except (OSError, TypeError, ValueError) as e:  # exception-ok: file system boundary; json reports unencodable entries only so
        _warn_journal(e)
        return
    _DEC_APPENDED = 0


def _warn_journal(e):
    """Schreibfehler des Journals gedrosselt melden (hoechstens alle 10 min); das Protokoll im Speicher lebt weiter."""
    global _DEC_WARNED
    if time.time() - _DEC_WARNED > 600:
        _DEC_WARNED = time.time()
        from .common import log
        log.warning("events.jsonl schreiben: %s", e)


def decisions_append(entry):
    """Eine Zeile anhaengen; nach 4 x DECISIONS_KEEP Zeilen eindampfen, damit die Datei nicht unbegrenzt waechst
    (~300 B je Eintrag: hoechstens ~2,5 MB). Schreibfehler werden gedrosselt gemeldet, das Protokoll im Speicher lebt weiter."""
    global _DEC_APPENDED
    if not DECISIONS_FILE:
        return
    import json
    try:
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as e:  # exception-ok: file system boundary; json reports unencodable entries only so
        _warn_journal(e)
        return
    _DEC_APPENDED += 1
    if _DEC_APPENDED >= 4 * DECISIONS_KEEP:
        _decisions_compact()


def remember(entry):
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry["t"] = time.time()
    if entry.get("event") in ("busy", "free", "wol", "no_node", "context_truncated"):
        MQTT_DIRTY.append(True)
    DECISIONS.append(entry)
    del DECISIONS[:-DECISIONS_KEEP]
    decisions_append(entry)
    from . import metrics   # spaet, um Importzyklen zu vermeiden
    metrics.count_event(entry)
