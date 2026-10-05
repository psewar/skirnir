"""Polling der Knoten, Zustandsautomat free/busy, prewarm und Sicherheitsnetz."""

import asyncio
import time

from aiohttp import ClientError, ClientTimeout, web

from . import agentupdate, nodes, ollamaupdate, perf, state
from .common import GIB, log


async def poll_node(node):
    t = ClientTimeout(total=4)
    try:
        async with nodes.nreq(node, "get", "/api/tags", timeout=t) as r:
            tags = await r.json()
        async with nodes.nreq(node, "get", "/api/ps", timeout=t) as r:
            ps = await r.json()
    except (ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
        node.misses += 1
        if node.misses >= state.CFG.offline_after and node.state != "offline":
            node.go_offline(type(e).__name__)
        return
    node.misses = 0
    node.polled_ok = time.time()
    apply_ollama_state(node, tags.get("models", []), ps.get("models", []), time.time())
    if time.time() - node.version_ts > 300:   # beim Push kommt die Version mit dem Heartbeat
        node.version_ts = time.time()
        state.spawn(fetch_version(node))


def apply_ollama_state(node, tag_models, ps_models, now):
    """Ollama-Zustand eines Knotens uebernehmen: installierte (/api/tags) und geladene Modelle (/api/ps), Wechsel offline ->
    free, Faehigkeiten nachladen, Zustandsautomat. Gemeinsam fuer den Poll des Routers und die Meldung des Agenten
    (Rahmen OLLAMA, apply_ollama_push)."""
    node.models = {m["name"] for m in tag_models}
    node.model_details = {m["name"]: m for m in tag_models}
    if node.models:
        node.last_known_models = set(node.models)
        # ins Register, sonst weiss der Router nach einem Neustart bei schlafendem Knoten nicht, was der kann - und
        # wakeable_for() findet nichts zu wecken (2026-09-09 beim WOL-Nachweis gefunden: 404 statt Magic Packet)
        if node.reg is not None and node.reg.get("models") != sorted(node.models):
            node.reg["models"] = sorted(node.models)
            state.REG.dirty = True
    node.digest_of = {m["name"]: m.get("digest") for m in tag_models}
    node.loaded = {m["name"]: m.get("size_vram", 0) / GIB for m in ps_models}
    node.loaded_digest = {}
    node.loaded_ctx = {}
    for m in ps_models:
        d = m.get("digest") or node.digest_of.get(m["name"])
        if d:
            node.loaded_digest[d] = m.get("size_vram", 0) / GIB
            if m.get("context_length"):
                node.loaded_ctx[d] = int(m["context_length"])
    if node.state == "offline":
        log.info("node %s -> free (online)", node.name)
        node.state, node.free_at = "free", now
        node.breaker_reset()   # Stufe 3: alte Fehler eines abgestuerzten Knotens zaehlen nach dem Neustart nicht mehr
        state.MQTT_DIRTY.append(True)
        if state.CFG.prewarm_on_online:
            state.spawn(prewarm(node, state.CFG.prewarm_online_delay, "online", since=node.free_at))
    for m in node.models:
        if m not in state.CAPS:
            state.CAPS[m] = None   # in Arbeit, kein Doppel-Fetch
            state.spawn(fetch_caps(node, m))
    node.note_loaded_changed(now)
    evaluate(node, now)
    busy_unload_check(node, now)


def busy_unload_check(node, now):
    """Sicherheitsnetz: ein grosses Modell, das trotz busy geladen ist (Race mit einem laufenden Load oder keep_alive -1 einer
    Anfrage, die vor dem busy startete), wird nachtraeglich entladen. Zeitgesteuert (unload_on_busy_interval_s), darum auch aus
    tick_loop jede Sekunde - lief bis 0.3.8 nach jedem Poll; mit dem Ollama-Push kaemen Daten nur noch bei einer Aenderung."""
    if node.state == "busy" and state.CFG.unload_on_busy and now - node.last_busy_unload >= state.CFG.unload_on_busy_interval_s:
        big = [m for m, gib in node.loaded.items() if gib >= 0.5 and not any(node.same_blob(m, ok) for ok in state.CFG.busy_ok_models)]
        if big:
            node.last_busy_unload = now
            log.info("node %s busy, aber %s geladen -> entlade", node.name, ", ".join(big))
            state.spawn(unload_big_models(node))


async def fetch_version(node):
    try:
        async with nodes.nreq(node, "get", "/api/version", timeout=ClientTimeout(total=5)) as r:
            d = await r.json()
        ollamaupdate.note_version(node, str(d.get("version") or "?"), source="poll")   # setzt node.ollama_version (0.3.0)
    except Exception as e:  # noqa: BLE001
        log.debug("version %s: %s", node.name, e)


async def fetch_caps(node, model):
    try:
        async with nodes.nreq(node, "post", "/api/show", json={"model": model}, timeout=ClientTimeout(total=10)) as r:
            d = await r.json()
        state.CAPS[model] = d.get("capabilities", [])
    except Exception as e:  # noqa: BLE001
        state.CAPS.pop(model, None)
        log.debug("caps %s@%s: %s", model, node.name, e)


def evaluate(node, now):
    """Zustandsautomat free <-> busy. Wird bei jedem Poll und jedem Heartbeat aufgerufen."""
    if node.state == "offline":
        return
    # Waehrend einer Messung (perf.measure_model) ruht er: dort wird absichtlich entladen und geladen, und bis der
    # Katalog echte Zahlen hat, erschiene Ollamas ungemeldeter Rest als fremdes VRAM - busy + unload_on_busy
    # wuerden das Messobjekt mitten in der Messung entladen.
    if any(m.get("node") == node.name for m in state.MEASURING.values()):
        return
    gpu_known = node.gpu_known(now)
    # Auslastung allein reicht nicht: ein weiterer Ollama-Client (lokaler Worker, Agenten-Framework) belegt kein fremdes
    # VRAM und darf den Knoten nicht "busy" machen. Spiele/Encoder belegen immer Speicher.
    util_hot = (gpu_known and node.gpu_util is not None and node.gpu_util >= node.busy_util_threshold() and node.inflight == 0
                and node.foreign_vram_gib() >= state.CFG.util_requires_foreign_gib)
    if util_hot:
        node.hot_since = node.hot_since or now
    else:
        node.hot_since = None
    hot_long = node.hot_since is not None and (now - node.hot_since) >= state.CFG.busy_sustain
    # Fremdes VRAM braucht ebenfalls ein Haltefenster: beim Modellwechsel meldet /api/ps kurz nichts,
    # während das VRAM noch belegt ist -> sonst Fehlalarm "busy" für die Dauer der Exit-Hysterese.
    foreign_now = gpu_known and node.foreign_vram_gib() >= node.busy_foreign_threshold()
    node.foreign_since = (node.foreign_since or now) if foreign_now else None
    foreign = node.foreign_since is not None and (now - node.foreign_since) >= state.CFG.foreign_sustain
    # Hysterese (0.6.8): busy BLEIBEN ist leichter als busy WERDEN. Bis 0.6.7 musste ein busy-Knoten die Eintrittsbedingung
    # (Auslastung ueber der Schwelle, ohne Unterbrechung ueber sustain_s) dauernd erfuellen - ein Spiel mit 31-44 % Last bei
    # 40 % Schwelle flatterte so alle 20-60 s zwischen busy und free (gpu-laptop, 2026-10-04). Solange das Spiel fremdes VRAM
    # haelt und die Last ueber der Austrittsschwelle liegt, bleibt der Knoten busy; Ladebildschirme darunter deckt below_for_s ab.
    keep = (node.state == "busy" and gpu_known and node.gpu_util is not None and node.gpu_util >= node.busy_exit_util_threshold()
            and node.foreign_vram_gib() >= state.CFG.util_requires_foreign_gib)
    # Spiel als Fakt vom Agenten (0.7.0 / Agent 0.17.0): belegt unabhaengig von VRAM und Last - Diablo IV bekam neben
    # qwen3.8@196k kaum VRAM und lastete die GPU kaum aus, trotzdem lief es (RAM 94 %). Kurz nach dem Start erst nach
    # game_sustain_s (Launcher-Blitzer), danach haelt es den Knoten busy, bis das Spiel weg ist.
    game = (state.CFG.busy_game and node.game_since is not None
            and (node.state == "busy" or now - node.game_since >= state.CFG.game_sustain))
    trigger = hot_long or foreign or keep or game
    if trigger:
        node.calm_since = None
        if game and node.state == "busy" and node.busy_reason != "game":
            node.busy_reason = "game"   # Grund nachziehen: ein Spiel ist die genaueste Erklaerung
            state.MQTT_DIRTY.append(True)
        if node.state == "free":
            node.state = "busy"
            node.busy_reason = "game" if game else ("gpu_util" if hot_long else "foreign_vram")
            log.info("node %s -> busy (%s util=%s foreign=%.1fGiB)", node.name, node.busy_reason,
                     node.gpu_util, node.foreign_vram_gib())
            state.remember({"event": "busy", "node": node.name, "reason": node.busy_reason})
            if state.CFG.unload_on_busy:
                state.spawn(unload_big_models(node))
    elif node.state == "busy":
        node.calm_since = node.calm_since or now
        # Nach einem Spiel laenger warten (Neustart des Spiels, Wechsel Launcher -> Spiel), sonst laedt prewarm dazwischen
        quiet = max(state.CFG.busy_exit_s, state.CFG.game_exit_s) if node.busy_reason == "game" else state.CFG.busy_exit_s
        if (now - node.calm_since) >= quiet:
            node.state, node.calm_since, node.busy_reason = "free", None, ""
            log.info("node %s -> free (calm)", node.name)
            state.remember({"event": "free", "node": node.name})
            node.free_at = now
            if state.CFG.prewarm_on_free:
                state.spawn(prewarm(node, state.CFG.prewarm_free_delay, "free", since=node.free_at))


async def unload_big_models(node):
    for m, gib in list(node.loaded.items()):
        if any(node.same_blob(m, ok) for ok in state.CFG.busy_ok_models):
            continue
        if gib < 0.5:
            continue   # liegt im RAM (num_gpu 0), belegt kein VRAM -> darf beim Spielen bleiben
        if any(cnt > 0 and node.same_blob(m, im) for im, cnt in node.inflight_models.items()):
            continue
        try:
            async with nodes.nreq(node, "post", "/api/generate", json={"model": m, "keep_alive": 0},
                                    timeout=ClientTimeout(total=30)) as r:
                await r.read()
            log.info("unloaded %s on %s (busy)", m, node.name)
        except Exception as e:  # noqa: BLE001
            log.warning("unload %s on %s failed: %s", m, node.name, e)


async def prewarm(node, delay, reason, since=None):
    """Rang-1-Modelle der Rollen (in Konfig-Reihenfolge) auf dem Knoten vorladen, soweit sie zusammen ins Budget passen.
    Ohne das wuerde 'warm zuerst' nach einer Spielphase dauerhaft beim kleinen busy_ok-Modell bleiben.
    `since` = der Wechsel auf free, fuer den dieses Vorwaermen geplant wurde. Kam seither ein neuer Wechsel (dazwischen busy
    oder offline), plant der sein eigenes; dieses entfaellt. Vorher feuerte ein altes Vorwaermen kurz nach dem naechsten free."""
    if delay:
        await asyncio.sleep(delay)
    if since is not None and (node.free_at != since or node.state != "free"):
        log.info("prewarm on %s entfaellt (%s): Zustand hat seit dem Planen gewechselt", node.name, reason)
        return
    if time.time() < node.draining_until or ollamaupdate.update_running(node):
        # 2026-10-01: waehrend eines Ollama-Updates lud das Vorwaermen qwen3.8 (131k), das der Tausch 30 s spaeter wieder beendete
        log.info("prewarm on %s entfaellt (%s): Update laeuft", node.name, reason)
        return
    # Erst den Heartbeat urteilen lassen. Ein frisch (neu) gestarteter Router haelt jeden erreichbaren Knoten zunaechst
    # fuer free; am 2026-09-06 lud er so qwen3.6 in ein von einem Spiel belegtes VRAM (Ueberlauf in den Shared Memory,
    # Spiel ruckelte). Warten, bis GPU-Daten da sind und kein fremdes VRAM anliegt; ohne Agent nach dem Haltefenster weiter.
    deadline = time.time() + 4   # Agent meldet alle 3 s; ohne Agent nach 4 s weiter wie bisher
    while time.time() < deadline:
        if node.state != "free":
            return
        now = time.time()
        if node.gpu_known(now) and node.foreign_since is None:
            break
        await asyncio.sleep(1)
    if node.state != "free" or node.foreign_since is not None:
        log.info("prewarm on %s uebersprungen (%s): fremdes VRAM %.1f GiB", node.name, reason, node.foreign_vram_gib())
        return
    if node.mem_pressure or node.game_since is not None:
        # Agent >= 0.17.0: Hauptspeicher knapp oder Spiel gemeldet (z. B. noch im game_sustain-Fenster) - kein Laden, das
        # den Speicher weiter fuellt; nach Spielende erst, wenn auch der Speicherdruck vorbei ist (kein Laden-Entladen-Pingpong)
        log.info("prewarm on %s uebersprungen (%s): %s", node.name, reason, "Speicherdruck" if node.mem_pressure else "Spiel laeuft")
        return
    now = time.time()
    # Budget = Karte minus Reserve minus dem, was WIRKLICH fremd ist (Desktop-Grundverbrauch + fremdes VRAM); Ollamas eigene
    # Modelle sind verdraengbar und zaehlen nicht. Vorher stand hier die volle Karte - auf gpu-laptop (16 GiB) wurde gemma4 neben
    # ein laufendes Spiel geladen.
    budget = node.vram_total_gib - state.CFG.reserve.get("free", 1.0) - node.baseline_gib() - node.foreign_vram_gib()
    planned, used = [], 0.0
    for role in state.CFG.roles.values():
        t = role["tiers"][0]
        m = t["model"]
        if m not in node.models or any(node.same_blob(m, p[0]) for p in planned):
            continue
        need = state.CFG.need_gib(m, t["num_ctx"], node)
        if node.is_loaded(m):
            used += node.loaded_size(m)
            planned.append((m, t["num_ctx"], True))
            continue
        if used + need > budget:
            continue
        used += need
        planned.append((m, t["num_ctx"], False))
    for m, ctx, warm in planned:
        if warm or node.state != "free":
            continue
        try:
            node.announce_load(m, ctx)
            body = {"model": m, "keep_alive": state.CFG.keep_alive.get("free", -1), "options": {"num_ctx": ctx}}
            async with nodes.nreq(node, "post", "/api/generate", json=body, timeout=ClientTimeout(total=300)) as r:
                await r.read()
            log.info("prewarm %s@%d on %s (%s)", m, ctx, node.name, reason)
            state.remember({"event": "prewarm", "node": node.name, "model": m, "ctx": ctx, "reason": reason})
        except Exception as e:  # noqa: BLE001
            log.warning("prewarm %s on %s failed: %s", m, node.name, e)
        finally:
            node.finish_load(m)
    await poll_node(node)
    if node.state == "busy" and state.CFG.unload_on_busy:
        # waehrend des Ladens busy geworden (Race mit unload_on_busy): gleich wieder raus
        log.info("prewarm on %s: Knoten wurde waehrend des Ladens busy, entlade wieder", node.name)
        await unload_big_models(node)


def _named(models):
    """Eintraege aus einer Agenten-Meldung, die wie Ollamas eigene aussehen (dict mit Namen); alles andere faellt weg."""
    return [m for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]


def apply_ollama_push(node, msg, now):
    """Ollama-Zustand vom Agenten (Agent >= 0.14.0, Rahmen OLLAMA): bei jeder Aenderung sofort und spaetestens alle 30 s.
    Solange die Meldung frisch ist, pollt der Router diesen Knoten nicht. `up: false` ist die ausdrueckliche Meldung des
    Agenten, dass sein Ollama nicht antwortet - der Router muss das nicht mehr aus fehlgeschlagenen Abfragen erschliessen."""
    if not isinstance(msg, dict) or not isinstance(msg.get("up"), bool):
        node.ollama_push_ts = 0.0   # unbrauchbare Meldung: bis zur naechsten gueltigen wieder Poll, nicht die letzte behalten
        log.warning("node %s: Ollama-Zustand vom Agenten ohne 'up' - verworfen, Router pollt", node.name)
        return
    # Agent 0.14.0 schickte eine nie gesetzte leere Liste als null (Go: nil-Slice) - gleichbedeutend mit []. Ab 0.14.1 immer [].
    tags = [] if msg.get("tags") is None else msg.get("tags")
    ps = [] if msg.get("ps") is None else msg.get("ps")
    if msg["up"] and not (isinstance(tags, list) and isinstance(ps, list)):
        node.ollama_push_ts = 0.0
        log.warning("node %s: Ollama-Zustand vom Agenten ohne gueltige tags/ps - verworfen, Router pollt", node.name)
        return
    node.ollama_push_ts = now
    if not msg["up"]:
        node.ollama_push_error = str(msg.get("error") or "?")[:200]
        if node.state != "offline":
            node.go_offline(f"Ollama antwortet nicht laut Agent: {node.ollama_push_error}")
        return
    node.ollama_push_error = None
    apply_ollama_state(node, _named(tags), _named(ps), now)


async def poll_loop():
    while True:
        now = time.time()
        # Knoten, deren Agent den Ollama-Zustand frisch meldet, nicht abfragen; aeltere Agenten und statische Knoten weiter
        await asyncio.gather(*(poll_node(n) for n in state.NODES.values() if not n.ollama_push_active(now)), return_exceptions=True)
        await asyncio.sleep(state.CFG.poll_s)


def residency_check(node, now):
    """Rang-1 wieder vorwaermen, wenn ein Fremdmodell es verdraengt hat und seit `residency_idle_s` ungenutzt ist.

    Warum: "warm zuerst" nimmt die erste Stufe mit einem WARMEN Knoten. Verdraengt eine konkrete Anfrage (z. B. glm)
    das Rang-1-Modell auf dem Hauptknoten, ist die Ausweichstufe auf dem anderen Knoten warm und gewinnt dauerhaft -
    es gibt keinen Zustandswechsel, der prewarm ausloesen wuerde. Am 2026-09-10 lief so jede Sprach- und
    Standardanfrage auf gpu-laptop/gemma4:12b (69 statt 214 tok/s), bis jemand qwen von Hand zurueckholte.
    Fremdmodell = geladen, aber kein Rang-1-Modell irgendeiner Rolle. Rang-1-Modelle untereinander (coder verdraengt
    qwen) sind kein Fall fuer die Regel: die wuerde nur hin- und herladen. Kein Ping-Pong mit dem Verdraenger: solange
    er ueber den Router genutzt wird, bleibt er. Nichts geladen = Sache von prewarm on_free/on_online."""
    cfg = state.CFG
    if node.state != "free":
        node.free_since = 0.0
        return
    node.free_since = node.free_since or now
    if not cfg.residency_idle_s or node.inflight or node.loading or not node.gpu_known(now) or not node.loaded:
        return
    # Dieselbe Wartezeit wie prewarm on_free: ein kurz pausiertes Spiel soll das VRAM nicht sofort verlieren
    if now - node.free_since < cfg.prewarm_free_delay or now - node.last_residency < cfg.residency_check_s:
        return
    node.last_residency = now
    rank1 = []
    for r in cfg.roles.values():
        t = r["tiers"][0]
        if t["model"] in node.models and not any(node.same_blob(t["model"], m) for m, _ in rank1):
            rank1.append((t["model"], t["num_ctx"]))
    if not rank1:
        return
    displacers = [m for m in node.loaded if not any(node.same_blob(m, r) for r, _ in rank1)]
    missing = [(m, ctx) for m, ctx in rank1 if not node.is_loaded(m)]
    if not displacers or not missing:
        return
    if any(now - t < cfg.residency_idle_s for used, t in node.last_used.items() if any(node.same_blob(used, d) for d in displacers)):
        return
    # Nur, wenn ein fehlendes Rang-1-Modell neben den geladenen Rang-1-Modellen ueberhaupt Platz haette; sonst
    # wuerde prewarm ohnehin nichts laden und wir meldeten jede Minute dasselbe.
    room = node.vram_total_gib - cfg.reserve.get("free", 1.0) - sum(node.loaded_size(m) for m, _ in rank1 if node.is_loaded(m))
    if not any(cfg.need_gib(m, ctx, node) <= room for m, ctx in missing):
        return
    log.info("node %s: Rang-1 %s verdraengt durch %s, seit >%ds ungenutzt -> vorwaermen", node.name,
             ", ".join(m for m, _ in missing), ", ".join(displacers), int(cfg.residency_idle_s))
    state.spawn(prewarm(node, 0, "residency"))


async def tick_loop():
    """Re-evaluiert zwischen Polls, damit Sustain/Exit-Fenster auch ohne neue Daten ablaufen; sichert perf.json verzoegert."""
    while True:
        now = time.time()
        for n in state.NODES.values():
            evaluate(n, now)
            residency_check(n, now)
            busy_unload_check(n, now)
        if state.PERF_DIRTY[0] and now - state.PERF_DIRTY[0] >= 10:
            state.PERF_DIRTY[0] = 0.0
            await asyncio.to_thread(perf.perf_save)   # Dateischreiben nicht im Event-Loop
        if state.USAGE_DIRTY[0] and now - state.USAGE_DIRTY[0] >= 10:
            state.USAGE_DIRTY[0] = 0.0
            from . import metrics
            await asyncio.to_thread(metrics.usage_save)
        if state.METRICS_DIRTY[0] and now - state.METRICS_DIRTY[0] >= 60:   # metrics.METRICS_SAVE_S; Zaehler ueberleben Deploys
            state.METRICS_DIRTY[0] = 0.0
            from . import metrics
            await asyncio.to_thread(metrics.metrics_save)
        if state.REG is not None:
            state.REG.maybe_save(now)
        await asyncio.sleep(1)


async def handle_heartbeat(request):
    """Heartbeat-Einspeisung mit Token: GPU-Fakten fuer einen bekannten Knoten setzen, wie es der Agent durch den Tunnel tut.
    Kein Agentenweg mehr (der HTTP-Heartbeat des Agenten ist seit 0.9.1 weg), sondern Hebel fuer Tests und Diagnose;
    ohne router.heartbeat_token abgeschaltet."""
    if not state.CFG.heartbeat_token:
        return web.json_response({"error": "Heartbeat-Einspeisung abgeschaltet (router.heartbeat_token nicht gesetzt)"}, status=410)
    if request.headers.get("X-Router-Token") != state.CFG.heartbeat_token:
        return web.json_response({"error": "bad token"}, status=401)
    name = request.match_info["node"]
    node = state.NODES.get(name)
    if node is None:
        return web.json_response({"error": f"unknown node {name}"}, status=404)
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid json"}, status=400)
    apply_heartbeat(node, b, time.time())
    return web.json_response(hb_ack(node))


def note_game_memory(node, b, now):
    """Agent >= 0.17.0 meldet ein laufendes Spiel und den Hauptspeicher als Fakt (kays Regel: Zustand melden statt aus
    Nebenwirkungen erraten). Anlass 2026-10-04: Diablo IV + qwen3.8@196k -> RAM 94 %, freier Commit 1,8 GB, ~900 Hard
    Faults/s; der Knoten blieb free, weil Diablo kaum VRAM bekam (kein fremdes VRAM) und der Router RAM nicht kannte.
    Fehlt ein Block, meldet der Agent es nicht (aelter, Linux ohne Spielerkennung): dann bleibt es beim alten Verhalten."""
    g = b.get("game")
    if isinstance(g, dict) and g.get("supported", True):
        was = bool((node.game or {}).get("running"))
        node.game = {k: g.get(k) for k in ("running", "name", "via", "since") if g.get(k) is not None}
        running = bool(g.get("running"))
        if running and node.game_since is None:
            node.game_since = now
        elif not running:
            node.game_since = None
        if running != was:
            log.info("node %s: Spiel %s%s", node.name, "laeuft" if running else "beendet", f" ({g.get('name')})" if running else "")
            state.remember({"event": "game", "node": node.name, "running": running, "name": g.get("name") if running else None})
            state.MQTT_DIRTY.append(True)
    elif g is None:
        node.game, node.game_since = None, None
    m = b.get("memory")
    if not isinstance(m, dict):
        node.memory, node.mem_low_count, node.mem_pressure = None, 0, False
        return
    node.memory = {k: m.get(k) for k in ("ram_available_gib", "commit_free_gib", "ram_total_gib", "commit_limit_gib") if m.get(k) is not None}
    grenze = state.CFG.memory_pressure["min_free_gib"]
    knapp = any(isinstance(m.get(k), (int, float)) and m[k] < grenze for k in ("ram_available_gib", "commit_free_gib"))
    node.mem_low_count = node.mem_low_count + 1 if knapp else 0
    druck = node.mem_low_count >= state.CFG.memory_pressure["polls"]
    if druck != node.mem_pressure:
        node.mem_pressure = druck
        log.info("node %s: Speicherdruck %s (RAM frei %s GiB, Commit frei %s GiB, Grenze %s GiB)", node.name,
                 "beginnt" if druck else "vorbei", m.get("ram_available_gib"), m.get("commit_free_gib"), grenze)
        state.remember({"event": "memory_pressure", "node": node.name, "on": druck, "ram_available_gib": m.get("ram_available_gib"),
                        "commit_free_gib": m.get("commit_free_gib")})
        state.MQTT_DIRTY.append(True)


def hb_ack(node):
    """Antwort auf einen Heartbeat (HTTP und Tunnel): Urteil des Routers plus ob er den GPU-Schutz dieses Knotens beachtet."""
    return {"state": node.state, "busy_reason": node.busy_reason,
            "gpu_guard_ack": bool(state.CFG.gpu_guard["enabled"] and node.guard_policy)}


def apply_heartbeat(node, b, now):
    """GPU-Fakten eines Heartbeats uebernehmen, Zustandsautomat laufen lassen, Baseline lernen."""
    node.hb_ts = now
    fp = b.get("ollama_tls_sha256")
    if fp and node.tls and node.tls_mode == "auto" and fp.lower() != node.tls_fp:
        try:
            node.set_fingerprint(fp)
            log.info("node %s: TLS-Fingerprint vom Agenten uebernommen (%s...)", node.name, node.tls_fp[:16])
        except ValueError as e:
            log.warning("node %s: %s", node.name, e)
    node.gpu_util = b.get("gpu_util_pct")
    tot = b.get("vram_total_mib"); used = b.get("vram_used_mib"); free = b.get("vram_free_mib")
    node.vram_total_reported_gib = tot / 1024 if tot is not None else None
    node.vram_used_gib = used / 1024 if used is not None else None
    node.vram_free_gib = free / 1024 if free is not None else None
    op = b.get("ollama_proc_mib")
    node.ollama_proc_gib = op / 1024 if op is not None else None
    kv = b.get("children_vram_mib")   # Agent >= 0.13.0: {kind: MiB} ohne Ollama; fehlt das Feld, gibt es keine solchen Kinder
    node.children_vram_gib = sum(v for v in kv.values() if isinstance(v, (int, float))) / 1024 if isinstance(kv, dict) else 0.0
    note_game_memory(node, b, now)   # Agent >= 0.17.0: Spiel und Hauptspeicher als Fakt
    sens = b.get("sensors")   # Agent >= 0.6.0: Temperatur, Leistung, Drosselung, GPU-Z-Werte (unveraendert durchgereicht)
    node.sensors = sens if isinstance(sens, dict) else None
    if isinstance(b.get("update"), dict):   # Agent >= 0.8.0: Zwischenstand eines Update-Auftrags
        agentupdate.note_report(node, b["update"], now)
    if b.get("ollama_version"):   # Agent >= 0.12.0: Ollama-Version frisch vom Knoten (nach einem Ollama-Update sofort sichtbar)
        ollamaupdate.note_version(node, str(b["ollama_version"]), now, source="agent")
    if isinstance(b.get("ollama_update"), dict):   # Agent >= 0.12.0: Zwischenstand eines Ollama-Update-Auftrags
        ollamaupdate.note_report(node, b["ollama_update"], now)
    g = b.get("gpu_guard")     # Agent >= 0.7.0: GPU-Schutz (Power-Limit, Hochlast-Stufe, Warnungen) - nur Flag neben dem Zustand
    if isinstance(g, dict):
        prev = node.guard or {}
        node.guard, node.guard_ts = g, now
        if g.get("state") != prev.get("state") or bool(g.get("problem")) != bool(prev.get("problem")):
            reason = g.get("grund") or ", ".join(g.get("warnungen") or [])
            log.info("node %s: GPU-Schutz %s%s%s", node.name, g.get("state"), f" ({reason})" if reason else "",
                     f" Limit {g.get('limit_w')} W" if g.get("limit_w") is not None else "")
            state.remember({"event": "gpu_guard", "node": node.name, "state": g.get("state"), "limit_w": g.get("limit_w"),
                            "problem": bool(g.get("problem")), "reason": reason})
            state.MQTT_DIRTY.append(True)
    if node.vram_total_reported_gib and not node.vram_total_gib:
        node.vram_total_gib = node.vram_total_reported_gib
    # Baseline nur lernen, wenn Ollama nichts im VRAM haelt und keine Anfrage laeuft: dann ist "belegt" der reine
    # Desktop-Grundverbrauch (plus ggf. ein Spiel, das das 24-h-Minimum aber nicht senkt). Mit geladenem Modell waere
    # belegt-minus-size_vram unzuverlaessig (Ueberlauf in den Shared Memory verfaelscht size_vram).
    # Nur bei ruhiger GPU lernen: sonst wandert ein laufendes Spiel als "Desktop" in die Baseline (gpu-desktop hatte
    # 19 von 23 Stundeneimern bei 18,2 GiB). Der Filter haengt bewusst an gpu_util und NICHT am busy-Zustand -
    # der wird aus der Baseline berechnet, das waere ein Kreis: ein Knoten mit hohem Desktop-Verbrauch bliebe
    # fuer immer busy und koennte nie lernen, dass das sein Normalzustand ist.
    if (node.vram_used_gib is not None and node.ollama_vram_claimed_gib() < 0.5 and node.inflight == 0
            and node.gpu_util is not None and node.gpu_util < node.busy_util_threshold()
            and node.ps_fresh(now)):   # nur mit frischem /api/ps-Wissen (sonst zaehlt ein geladenes Modell als Desktop)
        node.learn_baseline(now, node.vram_used_gib - node.children_vram_gib)   # die Kinder zieht foreign_vram_gib schon ab
    evaluate(node, now)
