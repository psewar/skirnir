"""Settings that the Web-UI is allowed to modify (Operator request 2026-09-10: "most configurations also via the UI").

The UI does not write config.yaml, but a flat block `settings: {"<path>": value}` in roles.yaml (the override file).
`config.Config.merged()` applies it over config.yaml when loading. Only what is listed in EDITABLE is allowed - everything the
Router reads at RUNTIME. Deliberately excluded: Ports/TLS/Basic-Auth/MQTT/Audit-Log (startup values), Identities
(token_sha256 - Secrets are only created via POST /admin/clients/<name>/token, plaintext shown once), Provider endpoints and
Key sources (kind, base_url, api_key_env, secrets_file), Egress-Allowlist. Source IPs per Client have been editable since the Clients-Tab
(Operator request: manage Clients including Identity in the UI).
They remain in config.yaml and come via Deploy - a UI session should not be able to shift the trust basis.

Paths with `*` apply per entry (Cloud-Provider, Client) - only for names already known in config.yaml.
"""

import re

from . import values
from .common import DATA_CLASSES_DEFAULT, PRIORITIES, THINK_MODES  # noqa: E402
from .outcome import failed, invalid  # noqa: E402
SCORE_KEYS = ("warm", "inflight", "saturated", "vram_free", "weight", "speed", "errors", "half_open", "switch")

# typ: int | float | bool | str | choice | list | iplist | keep_alive | tristate
# choices "data_classes" / "roles" are resolved at runtime. nullable: null = "not set" (Router-Default / Inheritance).
EDITABLE = {}


def _e(path, typ, label, help_="", group="", default=None, label_en=None, help_en=None, **kw):
    """label/help in German, label_en/help_en in English (view(lang="en") returns them under the same keys)."""
    EDITABLE[path] = {"type": typ, "label": label, "help": help_, "group": group, "default": default,
                      "label_en": label_en if label_en is not None else label, "help_en": help_en if help_en is not None else help_, **kw}


G = "Cloud"
_e("router.cloud.enabled", "bool", "Cloud global", "aus = keine Cloud-Stufe kommt zum Zug, egal was Rollen erlauben", G, False,
   label_en="Cloud globally", help_en="off = no cloud tier is ever used, whatever roles allow")
_e("router.cloud.default_data_class", "choice", "Datenklasse ohne Angabe", "gilt fuer Anfragen ohne routing.data_class und Clients ohne data_class; personal = nie Cloud", G, "personal", choices="data_classes",
   label_en="Data class when unspecified", help_en="applies to requests without routing.data_class and clients without data_class; personal = never cloud")
_e("router.cloud.max_cloud_data_class", "choice", "Cloud bis Datenklasse", "hoechste Klasse, die noch in die Cloud darf", G, "internal", choices="data_classes",
   label_en="Cloud up to data class", help_en="highest class still allowed to go to the cloud")
_e("router.cloud.credential_scan", "choice", "Credential-Scan", "block = Prompts mit erkennbaren Schluesseln/Passwoertern gehen nicht in die Cloud", G, "block", choices=["block", "off"],
   label_en="Credential scan", help_en="block = prompts with recognisable keys/passwords never go to the cloud")
P = "router.cloud.providers.*."
_e(P + "enabled", "bool", "aktiv", "deaktiviert = Anbieter faellt als Stufe weg", "Cloud-Anbieter", True,
   label_en="enabled", help_en="disabled = provider drops out as a tier")
_e(P + "budget_month_chf", "float", "Budget CHF/Monat", "0 = kein Budget; erschoepft = Anbieter faellt als Stufe weg", "Cloud-Anbieter", 0, min=0, max=100000, step=0.5,
   label_en="Budget CHF/month", help_en="0 = no budget; exhausted = provider drops out as a tier")
_e(P + "warn_at_percent", "int", "Warnung ab %", "HA-Problem 'Cloud <anbieter>: x % des Monatsbudgets'", "Cloud-Anbieter", 80, min=1, max=100,
   label_en="Warn from %", help_en="HA problem 'Cloud <provider>: x % of the monthly budget'")
_e(P + "max_data_class", "choice", "Datenklasse max.", "eigene Obergrenze dieses Anbieters (leer = globale Grenze)", "Cloud-Anbieter", None, choices="data_classes", nullable=True,
   label_en="Data class max.", help_en="this provider's own ceiling (empty = global limit)")
_e(P + "timeout_s", "float", "Timeout s", "leer = request_timeout_s", "Cloud-Anbieter", None, min=5, max=3600, nullable=True,
   label_en="Timeout s", help_en="empty = request_timeout_s")
_e(P + "max_inflight", "int", "gleichzeitig", "Admission-Grenze fuer den Anbieter", "Cloud-Anbieter", 8, min=1, max=64,
   label_en="concurrent", help_en="admission limit for this provider")
_e(P + "region", "str", "Region", "nur Anzeige (Datenresidenz)", "Cloud-Anbieter", None, nullable=True, maxlen=32,
   label_en="Region", help_en="display only (data residency)")

G = "Decision Engine"
_e("decision_engine.enabled", "bool", "Auto-Rolle aktiv", "aus = auto:latest verschwindet aus /api/tags; Konfiguration bleibt", G, False,
   label_en="Auto role enabled", help_en="off = auto:latest disappears from /api/tags; configuration is kept")
_e("decision_engine.default", "str", "Standard-Rolle", "gilt, wenn alle Engines unsicher sind oder ausfallen (muss in options stehen)", G, None, nullable=True, maxlen=40,
   label_en="Default role", help_en="used when all engines are unsure or fail (must be listed in options)")
_e("decision_engine.policy.min_top_probability", "float", "sicher ab p(Top 1)", "darunter fragt die naechste Engine der Kette", G, 0.5, min=0, max=1, step=0.05,
   label_en="confident from p(top 1)", help_en="below this the next engine in the chain is asked")
_e("decision_engine.policy.min_margin", "float", "sicher ab Abstand Top 1 - Top 2", "0,46 zu 0,43 ist unsicher, 0,82 zu 0,07 nicht", G, 0.2, min=0, max=1, step=0.05,
   label_en="confident from margin top 1 - top 2", help_en="0.46 vs 0.43 is unsure, 0.82 vs 0.07 is not")
_e("decision_engine.policy.max_entropy_ratio", "float", "sicher bis Entropie", "normierte Entropie der Verteilung (0 = sicher, 1 = gleichverteilt)", G, 0.75, min=0, max=1, step=0.05,
   label_en="confident up to entropy", help_en="normalised entropy of the distribution (0 = certain, 1 = uniform)")

G = "Scheduler"
for k, lbl, lbl_en in (("warm", "warm (Bonus)", "warm (bonus)"), ("inflight", "je laufende Anfrage (Malus)", "per running request (penalty)"),
                       ("saturated", "voller Knoten (Malus)", "full node (penalty)"), ("vram_free", "freies VRAM ×", "free VRAM ×"),
                       ("weight", "Knotengewicht ×", "node weight ×"), ("speed", "Tempo ×", "speed ×"), ("errors", "Fehlerrate (Malus)", "error rate (penalty)"),
                       ("half_open", "Breaker-Probe (Malus)", "breaker probe (penalty)"),
                       ("switch", "Umschalten auf Standby-Ollama (Malus)", "switch to the standby Ollama (penalty)")):
    _e(f"modes.score.{k}", "float", lbl, "Gewicht im Kandidaten-Score (scheduler.score)", G,
       {"warm": 100.0, "inflight": 10.0, "saturated": 50.0, "vram_free": 2.0, "weight": 1.0, "speed": 2.0, "errors": 20.0, "half_open": 5.0,
        "switch": 60.0}[k], min=0, max=10000,
       label_en=lbl_en, help_en="weight in the candidate score (scheduler.score)")
_e("modes.warm_first", "bool", "warm zuerst (global)", "ein geladenes Modell der Rangliste schlaegt einen Kaltstart; je Rolle uebersteuerbar", G, True,
   label_en="warm first (global)", help_en="a loaded model from the ranking beats a cold start; can be overridden per role")
_e("modes.fit_overhead_gib", "float", "VRAM-Reserve je Modell GiB", "Aufschlag auf Gewichte + Kontext beim Passt-es-Test", G, 0.8, min=0, max=8, step=0.1,
   label_en="VRAM reserve per model GiB", help_en="added to weights + context in the fit test")

G = "Admission"
_e("modes.admission.max_inflight_default", "int", "gleichzeitig je Knoten", "Default, wenn die Knoten-Policy nichts sagt; sollte OLLAMA_NUM_PARALLEL entsprechen", G, 2, min=1, max=32,
   label_en="concurrent per node", help_en="default when the node policy says nothing; should match OLLAMA_NUM_PARALLEL")
_e("modes.admission.aging_s", "float", "Alterung s", "je so viele Sekunden Wartezeit eine Prioritaetsklasse aufwaerts", G, 30, min=1, max=3600,
   label_en="Aging s", help_en="one priority class up per this many seconds of waiting")
_e("modes.admission.max_wait_s", "float", "max. Wartezeit s (normal)", "Prioritaet normal wartet hoechstens so lange (503), wenn die Anfrage keine deadline_ms setzt; Agenten-Anfragen dauern oft Minuten", G, 600, min=1, max=7200,
   label_en="max. wait s (normal)", help_en="priority normal waits at most this long (503) unless the request sets deadline_ms; agent requests often take minutes")
_e("modes.admission.max_wait_interactive_s", "float", "max. Wartezeit s (interactive)", "Sprachbefehle u. a.: wer so lange wartet, ist ohnehin verloren", G, 60, min=1, max=3600,
   label_en="max. wait s (interactive)", help_en="voice commands etc.: a request waiting this long is lost anyway")
_e("modes.admission.max_wait_batch_s", "float", "max. Wartezeit s (batch)", "Nachtlauf, Messungen: darf lange hinter anderen warten", G, 1800, min=1, max=86400,
   label_en="max. wait s (batch)", help_en="night runs, measurements: may wait long behind others")
_e("modes.admission.max_queue", "int", "max. Warteschlange", "mehr Wartende -> 503 sofort", G, 64, min=1, max=10000,
   label_en="max. queue", help_en="more waiting -> 503 immediately")

G = "Circuit Breaker"
_e("modes.breaker.failures", "int", "Fehler bis open", "", G, 3, min=1, max=100, label_en="failures until open")
_e("modes.breaker.window_s", "float", "Fenster s", "Fehler zaehlen innerhalb dieses Fensters", G, 60, min=1, max=86400,
   label_en="Window s", help_en="failures count within this window")
_e("modes.breaker.open_s", "float", "offen s", "so lange ist der Knoten kein Kandidat, dann eine Probe", G, 30, min=1, max=86400,
   label_en="open s", help_en="the node is no candidate for this long, then one probe")

G = "GPU-Schutz"
_e("modes.gpu_guard.enabled", "bool", "Status beachten", "aus = der Router ignoriert den GPU-Schutz-Status aller Knoten (kein Deckel, kein Score-Abzug, kein HA-Problem); der Agent setzt sein Limit trotzdem", G, True,
   label_en="Honour status", help_en="off = the router ignores the GPU guard status of all nodes (no cap, no score penalty, no HA problem); the agent still sets its limit")
_e("modes.gpu_guard.throttled_max_inflight", "int", "Deckel in Stufe 2", "gleichzeitige Anfragen auf einem Knoten, dessen Agent 'gedrosselt' meldet", G, 1, min=1, max=32,
   label_en="Cap in stage 2", help_en="concurrent requests on a node whose agent reports 'throttled'")
_e("modes.gpu_guard.score_penalty", "float", "Score-Abzug Hochlast", "bei hochlast/gedrosselt; voller Knoten = 50", G, 40, min=0, max=1000,
   label_en="Score penalty high load", help_en="for high load/throttled; full node = 50")
_e("modes.gpu_guard.require_fresh_status", "bool", "ohne Status deckeln", "Knoten ohne frischen Schutzstatus (Agent < 0.7.0 oder schweigt) wie gedrosselt behandeln", G, False,
   label_en="Cap without status", help_en="treat nodes without a fresh guard status (agent < 0.7.0 or silent) as throttled")

G = "Agent-Update"
_e("modes.agent_update.enabled", "bool", "Rollout-Schleife", "aus = Updates nur von Hand (Knopf je Knoten); an = Knoten mit Policy 'Auto-Update' bekommen die Manifest-Version, Kanarienvogel zuerst", G, True,
   label_en="Rollout loop", help_en="off = updates by hand only (button per node); on = nodes with policy 'Auto-update' get the manifest version, canary first")
_e("modes.agent_update.canary", "str", "Kanarienvogel", "Knotenname, der neue Versionen zuerst bekommt; die anderen erst, wenn er sie lange genug sauber faehrt", G, "",
   label_en="Canary", help_en="node name that gets new versions first; the others only once it has run them cleanly long enough")
_e("modes.agent_update.canary_clean_h", "float", "... sauber seit h", "so lange muss der Kanarienvogel die Version fahren, bevor die anderen folgen", G, 24, min=0, max=720,
   label_en="... clean for h", help_en="the canary must run the version this long before the others follow")

G = "Ollama-Update"
_e("modes.ollama_update.enabled", "bool", "Ollama-Update", "an = Router prueft die neueste Ollama-Version und bringt Knoten mit Policy 'Ollama-Auto-Update' im Nachtfenster nach; aus = nur Anzeige und Knopf je Knoten", G, True,
   label_en="Ollama update", help_en="on = the router checks the latest Ollama version and updates nodes with policy 'Ollama auto-update' in the night window; off = display and per-node button only")
_e("modes.ollama_update.window_start", "str", "Fenster von (HH:MM)", "Ortszeit des Routers; leer = jederzeit", G, "02:00",
   label_en="Window from (HH:MM)", help_en="router local time; empty = any time")
_e("modes.ollama_update.window_end", "str", "Fenster bis (HH:MM)", "darf ueber Mitternacht gehen (z. B. 23:00 bis 05:00)", G, "05:00",
   label_en="Window until (HH:MM)", help_en="may cross midnight (e.g. 23:00 to 05:00)")
_e("modes.ollama_update.canary", "str", "Kanarienvogel", "Knotenname, der neue Ollama-Versionen zuerst bekommt; leer = der Kanarienvogel des Agent-Updates", G, "",
   label_en="Canary", help_en="node name that gets new Ollama versions first; empty = the agent update's canary")
_e("modes.ollama_update.canary_clean_h", "float", "... sauber seit h", "so lange muss der Kanarienvogel die Version fahren, bevor die anderen folgen", G, 24, min=0, max=720,
   label_en="... clean for h", help_en="the canary must run the version this long before the others follow")
_e("modes.ollama_update.timezone", "str", "Zeitzone des Fensters", "IANA-Name, z. B. Europe/Zurich; leer = Ortszeit des Routers (Container laufen oft auf UTC)", G, "",
   label_en="Window time zone", help_en="IANA name, e.g. Europe/Zurich; empty = router local time (containers often run on UTC)")
_e("modes.ollama_update.check_interval_h", "float", "Pruefung alle h", "wie oft der Router die neueste Version erfragt", G, 6, min=1, max=168,
   label_en="Check every h", help_en="how often the router asks for the latest version")
_e("modes.ollama_update.release_url", "str", "Release-Quelle", "GitHub-API des neuesten Releases (tag_name, assets, sha256sum.txt); der Agent laedt von seiner eigenen festen Quelle", G, "https://api.github.com/repos/ollama/ollama/releases/latest",
   label_en="Release source", help_en="GitHub API of the latest release (tag_name, assets, sha256sum.txt); the agent downloads from its own fixed source")

G = "OpenAI-Server"   # Router 0.16.0: an OpenAI-compatible server (Strata) only in a window or on request
_e("modes.upstream_window.enabled", "bool", "Nur im Fenster oder auf Anfrage",
   "an = ausserhalb des Fensters haelt der Router einen ungenutzten OpenAI-Server an, der Standby-Ollama bedient; eine Anfrage an das Modell des Servers startet ihn trotzdem. Nur fuer Knoten mit upstream_fallback",
   G, False, label_en="Only in the window or on request",
   help_en="on = outside the window the router holds an unused OpenAI server and the standby Ollama serves; a request for the server's model still starts it. Only for nodes with upstream_fallback")
_e("modes.upstream_window.window_start", "str", "Fenster von (HH:MM)", "darf ueber Mitternacht gehen", G, "01:00",
   label_en="Window from (HH:MM)", help_en="may cross midnight")
_e("modes.upstream_window.window_end", "str", "Fenster bis (HH:MM)", "z. B. 01:00 bis 07:00", G, "07:00",
   label_en="Window to (HH:MM)", help_en="e.g. 01:00 to 07:00")
_e("modes.upstream_window.timezone", "str", "Zeitzone des Fensters", "IANA-Name, z. B. Europe/Zurich; leer = Ortszeit des Routers", G, "",
   label_en="Time zone of the window", help_en="IANA name, e.g. Europe/Zurich; empty = router local time")
_e("modes.upstream_window.idle_hold_s", "float", "ausserhalb anhalten nach Leerlauf (s)",
   "so lange muss das Modell des Servers ausserhalb des Fensters ungenutzt sein, bevor der Router ihn wieder anhaelt", G, 900, min=60, max=86400,
   label_en="hold outside after idle (s)", help_en="the server's model must be unused this long outside the window before the router holds it again")

G = "Busy-Erkennung"
_e("modes.busy_enter.gpu_util_pct", "float", "GPU-Auslastung %", "ab hier gilt der Rechner als beschaeftigt (Spiel) ...", G, 40, min=0, max=100,
   label_en="GPU utilisation %", help_en="from here the machine counts as busy (game) ...")
_e("modes.busy_enter.sustain_s", "float", "... anhaltend s", "", G, 10, min=0, max=3600, label_en="... sustained s")
_e("modes.busy_enter.util_requires_foreign_gib", "float", "... nur mit fremdem VRAM GiB", "Auslastung zaehlt nur, wenn zusaetzlich so viel fremdes VRAM belegt ist", G, 1.0, min=0, max=64, step=0.1,
   label_en="... only with foreign VRAM GiB", help_en="utilisation only counts if this much foreign VRAM is also in use")
_e("modes.busy_enter.or_foreign_vram_gib", "float", "ODER fremdes VRAM GiB", "ueber der gelernten Baseline des Knotens", G, 2.0, min=0, max=64, step=0.5,
   label_en="OR foreign VRAM GiB", help_en="above the node's learned baseline")
_e("modes.busy_enter.foreign_sustain_s", "float", "... anhaltend s", "", G, 15, min=0, max=3600, label_en="... sustained s")
_e("modes.busy_exit.below_for_s", "float", "Ruhe bis free s", "so lange muss es ruhig sein, bis der Knoten wieder free ist", G, 30, min=0, max=3600,
   label_en="Quiet until free s", help_en="it must stay quiet this long before the node is free again")
_e("modes.busy_exit.gpu_util_pct", "float", "busy bleibt ueber GPU-Auslastung %",
   "Hysterese: einmal busy, gilt der Rechner als beschaeftigt, solange die Auslastung ueber diesem Wert liegt und fremdes VRAM belegt ist (hoechstens die Eintrittsschwelle)",
   G, 20, min=0, max=100, label_en="busy stays above GPU load %",
   help_en="hysteresis: once busy, the machine counts as busy while the load stays above this value and foreign VRAM is in use (at most the entry threshold)")
_e("modes.busy_enter.game", "bool", "Spiel macht busy", "Agent >= 0.17.0 meldet ein laufendes Spiel: der Knoten wird busy, unabhaengig von VRAM und Last",
   G, True, label_en="Game makes busy", help_en="agent >= 0.17.0 reports a running game: the node becomes busy regardless of VRAM and load")
_e("modes.busy_enter.game_sustain_s", "float", "... Spiel seit s", "Launcher-Blitzer nicht werten", G, 10, min=0, max=600,
   label_en="... game running for s", help_en="ignore launcher blips")
_e("modes.busy_enter.game_min_gpu_gib", "float", "... und Spiel braucht mind. GiB Grafikspeicher",
   "Agent >= 0.19.0 meldet den GPU-Bedarf des Spiels: busy erst ab so viel eigenem + geteiltem Grafikspeicher (oder ab der "
   "Engine-Auslastung unten, oder bei Speicherdruck). Geteilter Speicher zaehlt mit, weil ein verdraengtes Spiel dorthin ausweicht",
   G, 2.0, min=0, max=64, label_en="... and the game needs at least GiB of graphics memory",
   help_en="agent >= 0.19.0 reports the game's GPU use: busy only from this much dedicated + shared graphics memory (or the engine "
           "load below, or memory pressure). Shared memory counts because a game pushed out of VRAM moves there")
_e("modes.busy_enter.game_min_gpu_pct", "float", "... oder Spiel lastet eine GPU-Engine mind. % aus", "hoechste Engine-Auslastung "
   "der Spielprozesse (wie im Task-Manager)", G, 10.0, min=0, max=100, label_en="... or the game loads a GPU engine to at least %",
   help_en="highest engine load of the game's processes (as in Task Manager)")
_e("modes.busy_exit.game_quiet_s", "float", "nach Spielende free nach s", "Spielneustart/Launcherwechsel ueberbruecken, bevor vorgewaermt wird",
   G, 60, min=0, max=3600, label_en="free after game ends, s", help_en="bridge game restarts/launcher switches before prewarming")
_e("modes.memory_pressure.min_free_gib", "float", "Speicherdruck unter GiB frei",
   "RAM- oder Commit-frei darunter (Agent >= 0.17.0): nichts kalt laden, nicht vorwaermen; Geladenes antwortet weiter",
   G, 8, min=0, max=512, step=0.5, label_en="Memory pressure below GiB free",
   help_en="RAM or commit free below this (agent >= 0.17.0): no cold loads, no prewarm; loaded models keep answering")
_e("modes.memory_pressure.polls", "int", "... Heartbeats in Folge", "Ladespitzen ignorieren", G, 2, min=1, max=100,
   label_en="... heartbeats in a row", help_en="ignore load spikes")
_e("modes.unload_on_busy", "bool", "Modelle bei busy entladen", "", G, True, label_en="Unload models when busy")
_e("modes.unload_on_busy_interval_s", "float", "Entlade-Sicherheitsnetz alle s", "", G, 30, min=1, max=3600, label_en="Unload safety net every s")
_e("modes.vram_settle_s", "float", "VRAM-Nachlauf s", "bis nvidia-smi einen Unload nachvollzogen hat", G, 8, min=0, max=120,
   label_en="VRAM settle s", help_en="until nvidia-smi has caught up with an unload")
_e("modes.vram_reserve_gib.free", "float", "VRAM-Reserve free GiB", "bleibt beim Passt-es-Test frei", G, 1.0, min=0, max=32, step=0.5,
   label_en="VRAM reserve free GiB", help_en="kept free in the fit test")
_e("modes.vram_reserve_gib.busy", "float", "VRAM-Reserve busy GiB", "", G, 2.0, min=0, max=32, step=0.5, label_en="VRAM reserve busy GiB")
_e("modes.keep_alive.free", "keep_alive", "keep_alive free", "-1 = Modell bleibt geladen, sonst Dauer wie 5m/1h", G, -1,
   label_en="keep_alive free", help_en="-1 = model stays loaded, otherwise a duration like 5m/1h")
_e("modes.keep_alive.busy", "keep_alive", "keep_alive busy", "", G, "5m", label_en="keep_alive busy")

G = "Vorwaermen"
_e("modes.prewarm.on_free", "bool", "Rang-1 vorwaermen, wenn free", "", G, True, label_en="Prewarm rank 1 when free")
_e("modes.prewarm.free_delay_s", "float", "... nach s", "", G, 20, min=0, max=3600, label_en="... after s")
_e("modes.prewarm.on_online", "bool", "Rang-1 vorwaermen, wenn online", "", G, True, label_en="Prewarm rank 1 when online")
_e("modes.prewarm.online_delay_s", "float", "... nach s", "", G, 0, min=0, max=3600, label_en="... after s")
_e("modes.prewarm.residency_idle_s", "float", "Residenz: Fremdmodell ungenutzt seit s", "dann Rang-1 wieder vorwaermen; 0 = aus", G, 300, min=0, max=86400,
   label_en="Residency: foreign model idle for s", help_en="then prewarm rank 1 again; 0 = off")
_e("modes.prewarm.residency_check_s", "float", "Residenz-Pruefung alle s", "", G, 60, min=5, max=3600, label_en="Residency check every s")

G = "Anfragen & Limits"
_e("router.request_timeout_s", "float", "Timeout je Anfrage s", "", G, 600, min=5, max=7200, label_en="Timeout per request s")
_e("router.limits.max_images", "int", "max. Bilder", "413 vor dem Backend", G, 16, min=0, max=1000, label_en="max. images", help_en="413 before the backend")
_e("router.limits.max_tools", "int", "max. Tools", "", G, 128, min=0, max=10000, label_en="max. tools")
_e("router.limits.max_messages", "int", "max. Nachrichten", "", G, 1000, min=1, max=100000, label_en="max. messages")
_e("router.session_affinity_ttl_s", "float", "Session-Affinitaet s", "routing.session_id haelt die Sitzung so lange auf ihrem Knoten", G, 1800, min=0, max=86400,
   label_en="Session affinity s", help_en="routing.session_id keeps the session on its node this long")
_e("router.idempotency_ttl_s", "float", "Idempotency-Cache s", "", G, 600, min=0, max=86400, label_en="Idempotency cache s")
_e("router.openai.default_think", "tristate", "/v1: think ohne Angabe", "false = kein unsichtbares Nachdenken (Default), true = Modell-Default, leer = Ollama entscheidet", G, False,
   label_en="/v1: think when unspecified", help_en="false = no invisible thinking (default), true = model default, empty = Ollama decides")
_e("router.anthropic.keepalive_s", "float", "/v1/messages: Ping alle s", "SSE-Ping an Anthropic-Clients, solange der Knoten noch am Prompt rechnet", G, 15,
   min=1, max=120, label_en="/v1/messages: ping every s", help_en="SSE ping to Anthropic clients while the node is still processing the prompt")
_e("router.anthropic.think", "choice", "/v1/messages: Denken", "auto = wie der Client es verlangt (Claude Code: jeder Zug), on = immer, off = nie", G, "auto",
   choices=list(THINK_MODES), label_en="/v1/messages: thinking", help_en="auto = as the client asks (Claude Code: every turn), on = always, off = never")

G = "Knoten-Ueberwachung"
_e("router.ollama_poll_s", "float", "Ollama-Abfrage alle s", "/api/tags + /api/ps", G, 5, min=1, max=300, label_en="Ollama poll every s")
_e("router.toolcall_rescue", "bool", "Tool-Calls aus Text retten",
   "holt Aufrufe zurueck, die das Modell im falschen Dialekt geschrieben hat und Ollama still verwirft (gemessen: qwen3-coder:30b verliert oberhalb ~12k Token 80 %)", G, True,
   label_en="Rescue tool calls from text",
   help_en="recovers calls the model wrote in the wrong dialect and Ollama silently drops (measured: qwen3-coder:30b loses 80 % above ~12k tokens)")
_e("router.offline_after_misses", "int", "offline nach Fehlversuchen", "", G, 3, min=1, max=100, label_en="offline after failed polls")
_e("router.heartbeat_stale_s", "float", "Heartbeat veraltet nach s", "", G, 10, min=2, max=600, label_en="Heartbeat stale after s")
_e("router.heartbeat_interval_s", "float", "Heartbeat-Takt s", "gilt fuer Agenten bei ihrer naechsten Verbindung", G, 3, min=1, max=60, note="bei naechster Agent-Verbindung",
   label_en="Heartbeat interval s", help_en="applies to agents on their next connection", note_en="on next agent connection")
_e("router.agent_missing_problem_s", "float", "HA-Problem nach Agent-Stille s", "", G, 300, min=10, max=86400, label_en="HA problem after agent silence s")
_e("router.ram_spill_problem_gib", "float", "HA-Problem ab Modell im RAM GiB",
   "ein geladenes Modell, das Ollama teilweise in den Arbeitsspeicher legen musste, rechnet dort auf der CPU (vielfach langsamer); 0 = aus",
   G, 0.5, min=0, max=256, label_en="HA problem from model in RAM GiB",
   help_en="a loaded model that Ollama had to place partly in system RAM runs there on the CPU (many times slower); 0 = off")

G = "Wake-on-LAN"
_e("router.wol.wait_up_s", "float", "warten bis wach s", "", G, 90, min=5, max=900, label_en="wait until awake s")
_e("router.wol.retry_interval_s", "float", "Magic Packet alle s", "", G, 20, min=1, max=300, label_en="Magic packet every s")
_e("router.wol.cooldown_s", "float", "Abkuehlung s", "kein erneutes Wecken innerhalb dieser Zeit", G, 300, min=0, max=86400,
   label_en="Cooldown s", help_en="no further wake-up within this time")

G = "Client-Auth"
_e("router.client_auth.mode", "choice", "Modus", "observe = alles bedienen und zaehlen, enforce = 401 ohne Identitaet (dauerhaft, ersetzt den Laufzeit-Schalter)", G, "observe", choices=["observe", "enforce"],
   label_en="Mode", help_en="observe = serve and count everything, enforce = 401 without identity (persistent, replaces the runtime switch)")
C = "router.client_auth.clients.*."
_e(C + "ip", "iplist", "Quell-IPs", "Identitaet ueber die Absenderadresse fuer Clients, die keinen Header schicken koennen (Komma-getrennt); leer = nur Secret", "Clients", [], nullable=True,
   label_en="Source IPs", help_en="identity via the sender address for clients that cannot send a header (comma-separated); empty = secret only")
_e(C + "roles", "list", "Rollen", "erlaubte Rollen, * = alle", "Clients", ["*"], choices="roles", label_en="Roles", help_en="allowed roles, * = all")
_e(C + "models", "bool", "konkrete Modelle", "darf konkrete Modellnamen rufen", "Clients", True, label_en="concrete models", help_en="may call concrete model names")
_e(C + "requests_per_minute", "int", "Anfragen/min", "leer = unbegrenzt", "Clients", None, min=1, max=100000, nullable=True, label_en="Requests/min", help_en="empty = unlimited")
_e(C + "max_priority", "choice", "hoechste Prioritaet", "leer = keine Kappung", "Clients", None, choices=list(PRIORITIES), nullable=True,
   label_en="highest priority", help_en="empty = no cap")
_e(C + "cloud", "bool", "Cloud erlaubt", "aus = Opt-out: Cloud-Stufen werden fuer diesen Client uebersprungen", "Clients", True,
   label_en="Cloud allowed", help_en="off = opt-out: cloud tiers are skipped for this client")
_e(C + "data_class", "choice", "Datenklasse", "Deklaration fuer Anfragen ohne routing.data_class (leer = globaler Default)", "Clients", None, choices="data_classes", nullable=True,
   label_en="Data class", help_en="declaration for requests without routing.data_class (empty = global default)")

# Order in the Settings-Tab. Review 2026-09-25: Decision Engine, GPU guard and Agent-Update were missing here, their entries
# were never visible in the UI (saving via API worked).
GROUPS = ["Cloud", "Cloud-Anbieter", "Clients", "Scheduler", "Decision Engine", "Admission", "Circuit Breaker", "Busy-Erkennung", "Vorwaermen", "Anfragen & Limits",
          "Knoten-Ueberwachung", "GPU-Schutz", "Agent-Update", "Ollama-Update", "OpenAI-Server", "Wake-on-LAN", "Client-Auth"]
# English group names (view(lang="en")); covers GROUPS and all group-strings of the _e calls.
GROUPS_EN = {"Cloud": "Cloud", "Cloud-Anbieter": "Cloud providers", "Clients": "Clients", "Decision Engine": "Decision engine", "Scheduler": "Scheduler",
             "Admission": "Admission", "Circuit Breaker": "Circuit breaker", "GPU-Schutz": "GPU guard", "Agent-Update": "Agent update", "Ollama-Update": "Ollama update",
             "Busy-Erkennung": "Busy detection", "Vorwaermen": "Prewarming", "Anfragen & Limits": "Requests & limits", "Knoten-Ueberwachung": "Node monitoring",
             "Wake-on-LAN": "Wake-on-LAN", "Client-Auth": "Client auth", "OpenAI-Server": "OpenAI server"}

_KEEP_ALIVE = re.compile(r"^-?\d+(\.\d+)?[smh]?$")


def spec_for(path):
    """EDITABLE entry for a specific path (with name instead of *) or None."""
    if path in EDITABLE:
        return EDITABLE[path]
    parts = path.split(".")
    for tpl, spec in EDITABLE.items():
        tp = tpl.split(".")
        if len(tp) == len(parts) and all(a == "*" or a == b for a, b in zip(tp, parts, strict=True)):
            return spec
    return None


def _get(c, parts):
    cur = c
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return None, False
        cur = cur[p]
    return cur, True


def _classes(c):
    cl = ((c.get("router") or {}).get("cloud") or {})
    return [str(x) for x in (cl.get("data_classes") or DATA_CLASSES_DEFAULT)]


def coerce(path, value, c):
    """Check a value for a path and cast it to the type. A Problem 400 with plaintext if it doesn't fit; None
    only if nullable (hence check failed(), not for None)."""
    spec = spec_for(path)
    if spec is None:
        return invalid(f"settings.{path}: cannot be changed in the UI (config.yaml only)")
    if path == "router.client_auth.mode" and ((c.get("router") or {}).get("client_auth") or {}).get("locked"):
        return invalid("settings.router.client_auth.mode: locked (client_auth.locked in config.yaml) - change it by deploy")
    parts = path.split(".")
    if "*" in spec_key(path):
        # the named entry (Provider/Client) must exist in config.yaml - the UI does not create new ones
        parent, ok = _get(c, parts[:-1])
        if not ok or not isinstance(parent, dict):
            return invalid(f"settings.{path}: {'.'.join(parts[:-1])} does not exist in config.yaml")
    if value is None:
        if spec.get("nullable"):
            return None
        return invalid(f"settings.{path}: must not be empty")
    t = spec["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "an", "aus"):
            return value.lower() in ("true", "1", "an")
        return invalid(f"settings.{path}: true/false expected")
    if t in ("int", "float"):
        v = values.number(value, f"settings.{path}", integer=t == "int")
        if failed(v):
            return v
        lo, hi = spec.get("min"), spec.get("max")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return invalid(f"settings.{path}: {v} is not between {lo} and {hi}")
        return v
    if t == "choice":
        ch = spec["choices"]
        if ch == "data_classes":
            ch = _classes(c)
        if value not in ch:
            return invalid(f"settings.{path}: {value!r} is not one of the options {ch}")
        return value
    if t == "tristate":
        if value in (True, False, None):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "null", ""):
            return {"true": True, "false": False}.get(value.lower())
        return invalid(f"settings.{path}: true/false/empty expected")
    if t == "str":
        s = str(value).strip()
        if len(s) > spec.get("maxlen", 200):
            return invalid(f"settings.{path}: too long")
        return s
    if t == "list":
        if isinstance(value, str):
            value = [x.strip() for x in value.split(",") if x.strip()]
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
            return invalid(f"settings.{path}: list of names expected")
        if spec.get("choices") == "roles":
            known = set(c.get("roles") or {}) | {"*"}
            bad = [x for x in value if x not in known]
            if bad:
                return invalid(f"settings.{path}: unknown role(s) {bad}")
        return [x.strip() for x in value] or spec.get("default") or []
    if t == "iplist":
        if isinstance(value, str):
            value = [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]
        if not isinstance(value, list):
            return invalid(f"settings.{path}: list of IP addresses expected")
        out = [values.ip_address(x, f"settings.{path}") for x in value]
        problem = next((p for p in out if failed(p)), None)
        return problem or out or None
    if t == "keep_alive":
        if isinstance(value, bool):
            return invalid(f"settings.{path}: -1, 0 or a duration like 5m expected")
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip()
        if not _KEEP_ALIVE.match(s):
            return invalid(f"settings.{path}: -1, 0 or a duration like 5m/1h expected")
        return int(s) if s.lstrip("-").isdigit() else s
    return invalid(f"settings.{path}: unknown type {t}")


def spec_key(path):
    """The EDITABLE key (with *) that belongs to a specific path."""
    if path in EDITABLE:
        return path
    parts = path.split(".")
    for tpl in EDITABLE:
        tp = tpl.split(".")
        if len(tp) == len(parts) and all(a == "*" or a == b for a, b in zip(tp, parts, strict=True)):
            return tpl
    return path


def apply(c, settings):
    """settings {path: value} checked into the (not yet parsed) configuration c. Returns the checked values
    or the Problem of the first invalid one (then c is partially populated - callers work on a copy)."""
    if not isinstance(settings, dict):
        return invalid("settings must be a mapping path -> value")
    out = {}
    for path, value in settings.items():
        v = coerce(str(path), value, c)
        if failed(v):
            return v
        parts = str(path).split(".")
        cur = c
        for p in parts[:-1]:
            if cur.get(p) is None:
                cur[p] = {}
            cur = cur[p]
        if v is None:
            cur.pop(parts[-1], None)
        else:
            cur[parts[-1]] = v
        out[str(path)] = v
    return out


def view(cfg, base, overrides, lang="de"):
    """For the UI: all editable settings with value (effective), base value (config.yaml), override flag and metadata.
    cfg = parsed Config (raw = merged tree), base = config.yaml pure, overrides = settings block from roles.yaml.
    lang = "de" | "en": for "en", label/help/group/note and the group list carry the English texts (same keys)."""
    en = lang == "en"
    classes = cfg.cloud.get("data_classes") or DATA_CLASSES_DEFAULT
    roles = sorted(r["name"] for r in cfg.roles.values())
    items = []
    for tpl, spec in EDITABLE.items():
        if "*" in tpl:
            head, tail = tpl.split(".*.")
            container = _get(base, head.split("."))[0]
            names = sorted(container.keys()) if isinstance(container, dict) else []
            paths = [(f"{head}.{n}.{tail}", n) for n in names]
        else:
            paths = [(tpl, None)]
        for path, entity in paths:
            parts = path.split(".")
            val, ok = _get(cfg.raw, parts)
            bval, bok = _get(base, parts)
            choices = spec.get("choices")
            if choices == "data_classes":
                choices = list(classes)
            elif choices == "roles":
                choices = ["*"] + roles
            group = GROUPS_EN.get(spec["group"], spec["group"]) if en else spec["group"]
            label = (spec.get("label_en") or spec["label"]) if en else spec["label"]
            help_ = (spec.get("help_en") or spec["help"]) if en else spec["help"]
            note = (spec.get("note_en") or spec.get("note")) if en else spec.get("note")
            items.append({"path": path, "entity": entity, "group": group, "label": label, "help": help_, "type": spec["type"],
                          "choices": choices, "min": spec.get("min"), "max": spec.get("max"), "step": spec.get("step"), "nullable": bool(spec.get("nullable")),
                          "note": note, "default": spec.get("default"),
                          "value": val if ok else None, "set": ok, "base": bval if bok else None, "base_set": bok,
                          "overridden": path in (overrides or {})})
    groups = [GROUPS_EN.get(g, g) for g in GROUPS] if en else GROUPS
    return {"groups": groups, "items": items}
