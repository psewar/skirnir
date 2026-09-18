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
MEASURING = {}    # model -> {"node", "step", "started", "error"} waehrend einer laufenden Messung
PERF = {}         # "model@node" -> Leistungsdaten (passiv aus echten Anfragen + aktiver Benchmark), persistiert in perf.json
PERF_DIRTY = [0.0]


BENCHING = {}     # model -> {"node","step","started","error"}
SESSION = None
DECISIONS = []   # Ringpuffer der letzten Routing-Entscheidungen
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


def remember(entry):
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry["t"] = time.time()
    if entry.get("event") in ("busy", "free", "wol", "no_node"):
        MQTT_DIRTY.append(True)
    DECISIONS.append(entry)
    del DECISIONS[:-200]
    try:
        from . import metrics   # spaet, um Importzyklen zu vermeiden
        metrics.count_event(entry)
    except Exception:  # noqa: BLE001
        pass
