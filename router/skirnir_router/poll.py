"""Node polling, free/busy state machine, prewarming, and safety net."""

import asyncio
import time

from aiohttp import ClientError, ClientTimeout, web

from . import agentupdate, nodes, ollamaupdate, openai_node, perf, state
from .common import GIB, log, read_json

NODE_FAULTS = (ClientError, asyncio.TimeoutError, OSError, ValueError)   # Netz, Tunnel, Ollamas JSON (ValueError)


async def poll_node(node):
    t = ClientTimeout(total=4)
    try:
        if node.speaks_openai:   # one list for both (openai_node.models_to_ollama)
            async with nodes.nreq(node, "get", openai_node.MODELS_PATH, timeout=t) as r:
                tag_models, ps_models = openai_node.models_to_ollama(await r.json())
        else:
            async with nodes.nreq(node, "get", "/api/tags", timeout=t) as r:
                tag_models = (await r.json()).get("models", [])
            async with nodes.nreq(node, "get", "/api/ps", timeout=t) as r:
                ps_models = (await r.json()).get("models", [])
    except NODE_FAULTS as e:  # exception-ok: network boundary to the node and its JSON; a miss counts toward offline
        node.misses += 1
        if node.misses >= state.CFG.offline_after and node.state != "offline":
            node.go_offline(type(e).__name__)
        return
    node.misses = 0
    node.polled_ok = time.time()
    apply_ollama_state(node, tag_models, ps_models, time.time())
    if node.speaks_openai:   # no Ollama, no /api/version
        return
    if time.time() - node.version_ts > 300:   # on push, the version arrives with the heartbeat
        node.version_ts = time.time()
        state.spawn(fetch_version(node))


def ram_spill_gib(ps_entry):
    """GiB of a loaded model that Ollama placed in system RAM instead of VRAM (/api/ps: size - size_vram). Those layers run
    on the CPU and make every request many times slower (2026-10-22: 3.2 GiB in RAM, 8.8 instead of ~100 tok/s). 0 when
    the entry carries no sizes."""
    size, vram = ps_entry.get("size"), ps_entry.get("size_vram")
    if not (isinstance(size, (int, float)) and isinstance(vram, (int, float))):
        return 0.0
    return max(0.0, (size - vram) / GIB)


def apply_ollama_state(node, tag_models, ps_models, now):
    """Adopt Ollama state of a node: installed (/api/tags) and loaded models (/api/ps), transition offline ->
    free, reload capabilities, state machine. Shared for router polling and agent reporting
    (OLLAMA frame, apply_ollama_push)."""
    node.models = {m["name"] for m in tag_models}
    node.model_details = {m["name"]: m for m in tag_models}
    if node.models:
        node.last_known_models = set(node.models)
        # into the registry, otherwise the router doesn't know what a sleeping node can do after a restart - and
        # wakeable_for() findet nichts zu wecken (2026-09-09 beim WOL-Nachweis gefunden: 404 statt Magic Packet)
        if node.reg is not None and node.reg.get("models") != sorted(node.models):
            node.reg["models"] = sorted(node.models)
            state.REG.dirty = True
    node.digest_of = {m["name"]: m.get("digest") for m in tag_models}
    node.loaded = {m["name"]: m.get("size_vram", 0) / GIB for m in ps_models}
    spill = {m["name"]: g for m in ps_models if (g := ram_spill_gib(m)) >= nodes.RAM_SPILL_MIN_GIB}
    if spill.keys() != node.ram_spill.keys():
        state.MQTT_DIRTY.append(True)   # HA should see a model moving into or out of system RAM right away
    node.ram_spill = spill
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
        # 0.8.8: don't carry over the reason for busy before a failure - /admin/state otherwise showed "free, reason game"
        node.state, node.free_at, node.busy_reason, node.calm_since = "free", now, "", None
        node.breaker_reset()   # Stage 3: old errors of a crashed node no longer count after restart
        state.MQTT_DIRTY.append(True)
        if state.CFG.prewarm_on_online:
            state.spawn(prewarm(node, state.CFG.prewarm_online_delay, "online", since=node.free_at))
    for m in node.models:
        if m not in state.CAPS and not node.serves_openai(m):   # an OpenAI server has no /api/show: catalog capabilities apply
            state.CAPS[m] = None   # in progress, no double fetch
            state.spawn(fetch_caps(node, m))
    node.note_loaded_changed(now)
    evaluate(node, now)
    busy_unload_check(node, now)


def busy_unload_check(node, now):
    """Safety net: a large model that is loaded despite busy (race with a running Load or keep_alive -1 of a
    request that started before busy) is unloaded retroactively. Time-based (unload_on_busy_interval_s), hence also from
    tick_loop every second - ran after every poll until 0.3.8; with Ollama push, data would only arrive on change."""
    if node.state == "busy" and state.CFG.unload_on_busy and now - node.last_busy_unload >= state.CFG.unload_on_busy_interval_s:
        if node.speaks_openai:   # sizes are 0 there (memory counts as agent child): every loaded model counts
            big = openai_node.holdable(node) if node.upstream_releasable else []
        else:
            big = [m for m, gib in node.loaded.items() if gib >= 0.5 and not any(node.same_blob(m, ok) for ok in state.CFG.busy_ok_models)]
        if big:
            node.last_busy_unload = now
            log.info("node %s busy, but %s loaded -> unloading", node.name, ", ".join(big))
            state.spawn(unload_big_models(node))


async def fetch_version(node):
    try:
        async with nodes.nreq(node, "get", "/api/version", timeout=ClientTimeout(total=5)) as r:
            d = await r.json()
    except NODE_FAULTS as e:  # exception-ok: network boundary to the node and Ollama's JSON; the next poll asks again
        log.debug("version %s: %s", node.name, e)
        return
    if isinstance(d, dict):
        ollamaupdate.note_version(node, str(d.get("version") or "?"), source="poll")   # setzt node.ollama_version (0.3.0)


async def fetch_caps(node, model):
    try:
        async with nodes.nreq(node, "post", "/api/show", json={"model": model}, timeout=ClientTimeout(total=10)) as r:
            d = await r.json()
    except NODE_FAULTS as e:  # exception-ok: network boundary to the node and Ollama's JSON; unknown caps are asked again
        d = None
        log.debug("caps %s@%s: %s", model, node.name, e)
    caps = d.get("capabilities", []) if isinstance(d, dict) else None
    if isinstance(caps, list):
        state.CAPS[model] = caps
    else:
        state.CAPS.pop(model, None)


def evaluate(node, now):
    """free <-> busy state machine. Called on every poll and every heartbeat."""
    if node.state == "offline":
        return
    # During a measurement (perf.measure_model) it rests: there, unloading and loading happen intentionally, and until the
    # Katalog echte Zahlen hat, erschiene Ollamas ungemeldeter Rest als fremdes VRAM - busy + unload_on_busy
    # would unload the measurement object mid-measurement.
    if any(m.get("node") == node.name for m in state.MEASURING.values()):
        return
    gpu_known = node.gpu_known(now)
    # Utilization alone is not enough: another Ollama client (local worker, agent framework) does not occupy foreign
    # VRAM and must not make the node "busy". Games/encoders always occupy memory.
    util_hot = (gpu_known and node.gpu_util is not None and node.gpu_util >= node.busy_util_threshold() and node.inflight == 0
                and node.foreign_vram_gib() >= state.CFG.util_requires_foreign_gib)
    if util_hot:
        node.hot_since = node.hot_since or now
    else:
        node.hot_since = None
    hot_long = node.hot_since is not None and (now - node.hot_since) >= state.CFG.busy_sustain
    # Foreign VRAM also needs a hold window: on model switch, /api/ps briefly reports nothing,
    # while VRAM is still occupied -> otherwise false alarm "busy" for the duration of the exit hysteresis.
    foreign_now = gpu_known and node.foreign_vram_gib() >= node.busy_foreign_threshold()
    node.foreign_since = (node.foreign_since or now) if foreign_now else None
    foreign = node.foreign_since is not None and (now - node.foreign_since) >= state.CFG.foreign_sustain
    # Hysteresis (0.6.8): staying busy is easier than becoming busy. Until 0.6.7, a busy node had to continuously meet the entry condition
    # (utilization above threshold, without interruption for sustain_s) - a game with 31-44 % load at
    # 40 % threshold flapped between busy and free every 20-60 s (gpu-laptop, 2026-10-04). As long as the game holds foreign VRAM
    # and load is above the exit threshold, the node stays busy; loading screens below that are covered by below_for_s.
    keep = (node.state == "busy" and gpu_known and node.gpu_util is not None and node.gpu_util >= node.busy_exit_util_threshold()
            and node.foreign_vram_gib() >= state.CFG.util_requires_foreign_gib)
    # Game as fact from agent (0.7.0 / Agent 0.17.0): Diablo IV got hardly any VRAM next to qwen3.8@196k and barely loaded the GPU
    # yet it ran (RAM 94 %) - foreign VRAM and load alone don't detect that. Until 0.8.6, therefore, EVERY
    # game made it busy; a small one (735 MiB, 2 %) locked the whole card. Since 0.8.7, only a game that needs the GPU
    # (game_heavy, measured by agent >= 0.19.0, see game_needs_gpu). Shortly after start, only after game_sustain_s
    # (launcher blip), then it keeps the node busy until the game is gone.
    if node.game_since is not None and not node.game_heavy:
        heavy, why = game_needs_gpu(node)
        if heavy:
            node.game_heavy, node.game_heavy_why = True, why
            log.info("node %s: game %s needs the GPU (%s)", node.name, (node.game or {}).get("name"), why)
            state.remember({"event": "game_heavy", "node": node.name, "name": (node.game or {}).get("name"), "why": why})
        else:
            node.game_heavy_why = why
    game = (state.CFG.busy_game and node.game_since is not None and node.game_heavy
            and (node.state == "busy" or now - node.game_since >= state.CFG.game_sustain))
    trigger = hot_long or foreign or keep or game
    if trigger:
        node.calm_since = None
        if game and node.state == "busy" and node.busy_reason != "game":
            node.busy_reason = "game"   # Pull reason: a game is the most precise explanation
            state.MQTT_DIRTY.append(True)
        if node.state == "free":
            node.state = "busy"
            node.prewarm_after_game = False   # busy -> free prewarms anyway
            node.busy_reason = "game" if game else ("gpu_util" if hot_long else "foreign_vram")
            log.info("node %s -> busy (%s util=%s foreign=%.1fGiB)", node.name, node.busy_reason,
                     node.gpu_util, node.foreign_vram_gib())
            state.remember({"event": "busy", "node": node.name, "reason": node.busy_reason})
            if state.CFG.unload_on_busy:
                state.spawn(unload_big_models(node))
    elif node.state == "busy":
        node.calm_since = node.calm_since or now
        # Wait longer after a game (game restart, switch Launcher -> Game), otherwise prewarm interferes
        quiet = max(state.CFG.busy_exit_s, state.CFG.game_exit_s) if node.busy_reason == "game" else state.CFG.busy_exit_s
        if (now - node.calm_since) >= quiet:
            node.state, node.calm_since, node.busy_reason = "free", None, ""
            log.info("node %s -> free (calm)", node.name)
            state.remember({"event": "free", "node": node.name})
            node.free_at = now
            if state.CFG.prewarm_on_free:
                state.spawn(prewarm(node, state.CFG.prewarm_free_delay, "free", since=node.free_at))
    # 0.8.8: retry a prewarm that was blocked by an unassessed game (game_sustain window), as soon as the
    # game is deemed light or finished and the node remains free. Found 2026-10-21: after agent update, came
    # psewar online with running game, prewarm fell into the window, and the card stayed idle next to the light game.
    if (node.state == "free" and node.prewarm_after_game and not node.mem_pressure and not game_blocks_prewarm(node, now)):
        node.prewarm_after_game = False
        why = "game_light" if node.game_since is not None else "game_ended"
        log.info("node %s: prewarm caught up (%s)", node.name, why)
        state.spawn(prewarm(node, 0, why, since=node.free_at))


def game_blocks_prewarm(node, now):
    """Does the running game block prewarming? Yes, if it needs the GPU or is still in the game_sustain window (not
    yet assessed); a light game (0.8.7) does not - otherwise the card would stay idle next to it."""
    return node.game_since is not None and (node.game_heavy or now - node.game_since < state.CFG.game_sustain)


def game_needs_gpu(node):
    """(needs GPU?, reason) for the running game. Graphics memory = own + shared: a game that gets no VRAM next to a large
    model falls back to shared memory and still counts as heavy. Without reported
    need (Agent < 0.19.0, measurement failed), every game is considered heavy - behavior until 0.8.6."""
    g = node.game or {}
    ded, shared, pct = g.get("gpu_dedicated_mib"), g.get("gpu_shared_mib"), g.get("gpu_pct")
    if not isinstance(ded, (int, float)) or not isinstance(shared, (int, float)):
        return True, "GPU use not reported"
    gib = (ded + shared) / 1024
    if gib >= state.CFG.game_min_gpu_gib:
        return True, f"graphics memory {gib:.1f} GiB (dedicated {ded:.0f} MiB, shared {shared:.0f} MiB)"
    if isinstance(pct, (int, float)) and pct >= state.CFG.game_min_gpu_pct:
        return True, f"GPU engine {pct:.0f} %"
    if node.mem_pressure:
        return True, "memory pressure"
    return False, f"light: {gib:.1f} GiB, GPU engine " + (f"{pct:.0f} %" if isinstance(pct, (int, float)) else "no rate yet")


async def unload_big_models(node):
    if node.speaks_openai:
        # An OpenAI-compatible server cannot unload a model; the agent stops the whole server instead (openai_node.order).
        # Not while a request runs on it: the next check (unload_on_busy_interval_s) tries again.
        if openai_node.holdable(node) and not any(cnt > 0 for cnt in node.inflight_models.values()):
            await openai_node.order(node, "hold", "busy")
        return
    for m, gib in list(node.loaded.items()):
        if any(node.same_blob(m, ok) for ok in state.CFG.busy_ok_models):
            continue
        if gib < 0.5:
            continue   # is in RAM (num_gpu 0), occupies no VRAM -> may stay during gaming
        if any(cnt > 0 and node.same_blob(m, im) for im, cnt in node.inflight_models.items()):
            continue
        try:
            async with nodes.nreq(node, "post", "/api/generate", json={"model": m, "keep_alive": 0},
                                    timeout=ClientTimeout(total=30)) as r:
                await r.read()
            log.info("unloaded %s on %s (busy)", m, node.name)
        except NODE_FAULTS as e:  # exception-ok: network boundary to the node; the busy check unloads again on the next poll
            log.warning("unload %s on %s failed: %s", m, node.name, e)


async def prewarm(node, delay, reason, since=None):
    """Preload rank-1 models of roles (in config order) on the node, as long as they fit in the budget together.
    Without this, 'warm first' would permanently stay on the small busy_ok model after a game phase.
    `since` = the transition to free for which this prewarm was planned. If a new transition occurred since (busy
    or offline in between), that one plans its own; this one is dropped. Previously, an old prewarm fired shortly after the next free."""
    if delay:
        await asyncio.sleep(delay)
    if since is not None and (node.free_at != since or node.state != "free"):
        log.info("prewarm on %s dropped (%s): state changed since it was planned", node.name, reason)
        return
    if time.time() < node.draining_until or ollamaupdate.update_running(node):
        # 2026-10-01: during an Ollama update, prewarm loaded qwen3.8 (131k), which the release ended 30 s later
        log.info("prewarm on %s dropped (%s): update running", node.name, reason)
        return
    # Let the heartbeat judge first. A freshly (re)started router initially holds every reachable node
    # as free; on 2026-09-06, it thus loaded qwen3.6 into VRAM occupied by a game (overflow into Shared Memory,
    # game stuttered). Wait until GPU data is available and no foreign VRAM is present; without agent, continue after the hold window.
    deadline = time.time() + 4   # Agent reports every 3 s; without agent, continue after 4 s as before
    while time.time() < deadline:
        if node.state != "free":
            return
        now = time.time()
        if node.gpu_known(now) and node.foreign_since is None:
            break
        await asyncio.sleep(1)
    if node.state != "free" or node.foreign_since is not None:
        log.info("prewarm on %s skipped (%s): foreign VRAM %.1f GiB", node.name, reason, node.foreign_vram_gib())
        return
    # A game requiring the GPU or still under evaluation (game_sustain window) blocks prewarming; a light one
    # (0.8.7) does not - otherwise the card would sit idle next to a small game. If it needs the GPU later, it becomes heavy,
    # the node turns busy, and the large model is evicted. Blocked by a game: evaluate() retrieves it later (prewarm_after_game, 0.8.8).
    game_blocks = game_blocks_prewarm(node, time.time())
    if game_blocks:
        node.prewarm_after_game = True
    if node.mem_pressure or game_blocks:
        # Agent >= 0.17.0: RAM is tight or a game is reported (e.g., still in game_sustain window) - no loading, as that
        # would further fill memory; only after the game ends and memory pressure subsides (no load-evict ping-pong)
        log.info("prewarm on %s skipped (%s): %s", node.name, reason, "memory pressure" if node.mem_pressure else "game running")
        return
    if node.openai_upstream and openai_node.server_allowed(node, time.time()):
        # The server loads its own model when it starts; "prewarming" it = starting it again if it was stopped for the GPU.
        # Also while the standby Ollama stands in: the release stops the standby; nothing is to be loaded into it.
        if node.upstream_blocked:   # 0.17.0: the agent would refuse it; it clears the block once there is room
            log.info("prewarm on %s: server stays held (agent: %s)", node.name, node.upstream_blocked.get("reason"))
        elif node.upstream_state == openai_node.HELD or (
                node.upstream_order == "hold" and not any(node.serves_openai(m) for m in node.loaded)):
            await openai_node.order(node, "release", f"prewarm {reason}")
        return
    if node.openai_upstream and node.upstream_serving != openai_node.SERVING_OLLAMA:
        return   # 0.16.0: outside the window the server stays held; prewarm waits until the standby serves
    # From here an Ollama node - including the standby Ollama outside the window (its server's models are skipped below)
    now = time.time()
    # Budget = card minus reserve minus what is truly foreign (desktop baseline + foreign VRAM); Ollama's own
    # models are evictable and don't count. Previously this was the full card - on gpu-laptop (16 GiB) gemma4 was loaded next to
    # a running game.
    budget = node.vram_total_gib - state.CFG.reserve.get("free", 1.0) - node.baseline_gib() - node.foreign_vram_gib()
    planned, used = [], 0.0
    for role in state.CFG.roles.values():
        t = role["tiers"][0]
        m = t["model"]
        if m not in node.models or node.serves_openai(m) or any(node.same_blob(m, p[0]) for p in planned):
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
        except NODE_FAULTS as e:  # exception-ok: network boundary to the node; a failed prewarm leaves the model for the first request
            log.warning("prewarm %s on %s failed: %s", m, node.name, e)
        finally:
            node.finish_load(m)
    await poll_node(node)
    if node.state == "busy" and state.CFG.unload_on_busy:
        # became busy during loading (race with unload_on_busy): evict immediately
        log.info("prewarm on %s: node went busy while loading, unloading again", node.name)
        await unload_big_models(node)


def _named(models):
    """Entries from an agent report that look like Ollama's own (dict with name); everything else is dropped."""
    return [m for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]


POWER_STATES = {"suspending": "asleep", "resumed": "awake"}


def note_power(node, power, now, via):
    """Record a power change as a fact (journal event `power`), only when it changes."""
    if node.power == power:
        return
    node.power, node.power_since = power, now
    log.info("node %s: %s (%s)", node.name, power, via)
    state.remember({"event": "power", "node": node.name, "state": power, "via": via})


def apply_power(node, msg, now):
    """Sleep or wake-up notice from the agent (Agent >= 0.24.0, frame POWER). Before, the router only saw a ping
    timeout about a minute after the machine went to sleep, and short standby wake-ups looked like a flapping tunnel
    (a desktop node, 2026-10-23). A suspend takes the node out of routing at once; a wake-up waits for the next heartbeat."""
    if not isinstance(msg, dict) or msg.get("state") not in POWER_STATES:
        log.warning("node %s: power notice from the agent without a known state - dropped", node.name)
        return
    power = POWER_STATES[msg["state"]]
    note_power(node, power, now, str(msg.get("via") or "agent")[:40])
    if power == "asleep":
        node.go_offline("asleep (agent reported suspend)")


def apply_ollama_push(node, msg, now):
    """Ollama state from the agent (Agent >= 0.14.0, frame OLLAMA): immediately on change and at most every 30 s.
    While the report is fresh, the router does not poll this node. `up: false` is the agent's explicit report that its Ollama is not responding - the router no longer needs to infer this from failed requests."""
    if not isinstance(msg, dict) or not isinstance(msg.get("up"), bool):
        node.ollama_push_ts = 0.0   # Useless report: poll again until the next valid one, do not keep the last one
        log.warning("node %s: Ollama state from the agent without 'up' - dropped, the router polls", node.name)
        return
    # Agent 0.14.0 sent a never-set empty list as null (Go: nil-Slice) - equivalent to []. From 0.14.1 always [].
    tags = [] if msg.get("tags") is None else msg.get("tags")
    ps = [] if msg.get("ps") is None else msg.get("ps")
    if msg["up"] and not (isinstance(tags, list) and isinstance(ps, list)):
        node.ollama_push_ts = 0.0
        log.warning("node %s: Ollama state from the agent without valid tags/ps - dropped, the router polls", node.name)
        return
    node.ollama_push_ts = now
    if not msg["up"]:
        node.ollama_push_error = str(msg.get("error") or "?")[:200]
        if node.state != "offline":
            node.go_offline(f"Ollama not answering according to the agent: {node.ollama_push_error}")
        return
    node.ollama_push_error = None
    # OpenAI-compatible upstream (agent >= 0.22.0): "held" = stopped to free the GPU, "starting" = loading again
    us = msg.get("upstream_state")
    node.upstream_state = us if us in (openai_node.HELD, openai_node.STARTING) else ""
    # Standby Ollama in place of the held server (agent >= 0.23.0): until the release the node is an Ollama node
    serving = openai_node.SERVING_OLLAMA if msg.get("serving") == openai_node.SERVING_OLLAMA else ""
    if serving != node.upstream_serving:
        log.info("node %s: %s", node.name, "standby Ollama serves while the server is held" if serving
                 else "standby Ollama no longer serves")
        state.remember({"event": "upstream_standby", "node": node.name, "serving": serving or None})
        state.MQTT_DIRTY.append(True)
        node.standby_prewarmed = False
    node.upstream_serving = serving
    # Agent >= 0.26.0: it keeps the held server from starting and says why (RAM in use + the server's need over its limit)
    blocked = msg.get("upstream_blocked") if isinstance(msg.get("upstream_blocked"), dict) else None
    if bool(blocked) != bool(node.upstream_blocked):
        if blocked:
            log.warning("node %s: agent keeps the server held - %s: needs %s GiB, %s of %s GiB RAM available (limit %s %%)",
                        node.name, blocked.get("reason"), blocked.get("need_gib"), blocked.get("ram_available_gib"),
                        blocked.get("ram_total_gib"), blocked.get("limit_percent"))
        else:
            log.info("node %s: the server may start again (no longer blocked)", node.name)
        state.remember({"event": "upstream_blocked", "node": node.name, "blocked": blocked})
        state.MQTT_DIRTY.append(True)
    node.upstream_blocked = blocked
    apply_ollama_state(node, _named(tags), _named(ps), now)


async def poll_loop():
    while True:
        now = time.time()
        # Do not query nodes whose agent reports Ollama state fresh; continue polling older agents and static nodes
        await asyncio.gather(*(poll_node(n) for n in state.NODES.values() if not n.ollama_push_active(now)), return_exceptions=True)
        await asyncio.sleep(state.CFG.poll_s)


def residency_check(node, now):
    """Prewarm rank-1 again if a foreign model evicted it and it has been unused for `residency_idle_s`.

    Why: "warm first" picks the first tier with a WARM node. If a specific request (e.g., glm)
    evicts the rank-1 model on the main node, the fallback tier on the other node is warm and wins permanently -
    there is no state change to trigger prewarm. On 2026-09-10, every language and
    standard request ran on gpu-laptop/gemma4:12b (69 vs 214 tok/s) until someone manually restored qwen.
    Foreign model = loaded, but not a rank-1 model of any role. Rank-1 models among themselves (coder evicts
    qwen) are not a case for this rule: it would just load back and forth. No ping-pong with the evictor: as long as
    it is used via the router, it stays. Nothing loaded = matter for prewarm on_free/on_online."""
    cfg = state.CFG
    if node.state != "free":
        node.free_since = 0.0
        return
    node.free_since = node.free_since or now
    if not cfg.residency_idle_s or node.inflight or node.loading or not node.gpu_known(now) or not node.loaded:
        return
    # Same wait time as prewarm on_free: a briefly paused game should not lose VRAM immediately
    if now - node.free_since < cfg.prewarm_free_delay or now - node.last_residency < cfg.residency_check_s:
        return
    node.last_residency = now
    rank1 = []
    for r in cfg.roles.values():
        t = r["tiers"][0]
        if t["model"] in node.models and not any(node.same_blob(t["model"], m) for m, _ in rank1):
            if node.serves_openai(t["model"]) and not openai_node.server_allowed(node, now):
                continue   # 0.16.0: outside the window the server's model is no rank-1 model to bring back
            rank1.append((t["model"], t["num_ctx"]))
    if not rank1:
        return
    displacers = [m for m in node.loaded if not any(node.same_blob(m, r) for r, _ in rank1)]
    missing = [(m, ctx) for m, ctx in rank1 if not node.is_loaded(m)]
    if not displacers or not missing:
        return
    if any(now - t < cfg.residency_idle_s for used, t in node.last_used.items() if any(node.same_blob(used, d) for d in displacers)):
        return
    # Only if a missing rank-1 model would actually fit next to the loaded rank-1 models; otherwise
    # prewarm would load nothing anyway and we would report the same thing every minute.
    room = node.vram_total_gib - cfg.reserve.get("free", 1.0) - sum(node.loaded_size(m) for m, _ in rank1 if node.is_loaded(m))
    if not any(cfg.need_gib(m, ctx, node) <= room for m, ctx in missing):
        return
    log.info("node %s: rank 1 %s displaced by %s, unused for >%ds -> prewarm", node.name,
             ", ".join(m for m, _ in missing), ", ".join(displacers), int(cfg.residency_idle_s))
    state.spawn(prewarm(node, 0, "residency"))


WINDOW_ORDER_GAP_S = 30   # one window order per node at most this often (the agent ignores repeats anyway)


def upstream_window_check(node, now):
    """Router 0.16.0 (modes.upstream_window): an OpenAI-compatible server such as Strata takes most of the card, so it runs
    only in the window (nights) or on request. Outside the window a server idle for idle_hold_s is held and the standby
    Ollama serves; inside it a held server is released once the node is free and the standby has been idle as long, so a
    deliberate standby request at night does not ping-pong. A request for the server's model starts it at any time."""
    w = state.CFG.upstream_window
    if not w["enabled"] or not node.can_switch_to_standby or node.state == "offline" or node.inflight:
        return
    if now - node.window_order_at < WINDOW_ORDER_GAP_S:
        return
    inside = openai_node.server_allowed(node, now)
    idle_s = w["idle_hold_s"]
    if inside:
        standby_used = [t for m, t in node.last_used.items() if not node.serves_openai(m)]
        # 0.16.3: "held" is the agent's report, not the router's memory of its last order - that is empty after a router
        # restart, and the window then never released the server (2026-10-24, deploy at 00:22 before a 01:00 window)
        held = node.upstream_state == openai_node.HELD or node.upstream_serving == openai_node.SERVING_OLLAMA
        if (node.state == "free" and held and not node.upstream_blocked
                and not (standby_used and now - max(standby_used) < idle_s)):
            node.window_order_at = now
            state.spawn(openai_node.order(node, "release", "night window"))
        return
    if node.upstream_serving == openai_node.SERVING_OLLAMA:
        # 0.16.1: the hold leaves the standby empty, and neither trigger fills it: prewarm runs on online/free, residency
        # only when something is loaded. The first request of the day then loaded its model cold. Prewarm once per hold.
        if node.state == "free" and not node.loaded and not node.loading and not node.standby_prewarmed:
            node.standby_prewarmed = True
            state.spawn(prewarm(node, 0, "standby", since=node.free_at))
        return
    if not node.speaks_openai or node.upstream_order == "hold" or node.upstream_state:
        return   # the standby already serves, a hold is under way, or the server is still starting
    server_used = [t for m, t in node.last_used.items() if node.serves_openai(m)]
    if server_used and now - max(server_used) < idle_s:
        return
    node.window_order_at = now
    state.spawn(openai_node.order(node, "hold", "outside the night window"))


async def tick_loop():
    """Re-evaluates between polls so Sustain/Exit windows expire even without new data; saves perf.json delayed."""
    while True:
        now = time.time()
        for n in state.NODES.values():
            evaluate(n, now)
            residency_check(n, now)
            busy_unload_check(n, now)
            upstream_window_check(n, now)
        if state.PERF_DIRTY[0] and now - state.PERF_DIRTY[0] >= 10:
            state.PERF_DIRTY[0] = 0.0
            await asyncio.to_thread(perf.perf_save)   # File writes not in the event loop
        if state.USAGE_DIRTY[0] and now - state.USAGE_DIRTY[0] >= 10:
            state.USAGE_DIRTY[0] = 0.0
            from . import metrics
            await asyncio.to_thread(metrics.usage_save)
        if state.METRICS_DIRTY[0] and now - state.METRICS_DIRTY[0] >= 60:   # metrics.METRICS_SAVE_S; counters survive deploys
            state.METRICS_DIRTY[0] = 0.0
            from . import metrics
            await asyncio.to_thread(metrics.metrics_save)
        if state.REG is not None:
            state.REG.maybe_save(now)
        await asyncio.sleep(1)


async def handle_heartbeat(request):
    """Heartbeat injection with token: sets GPU facts for a known node, as the agent does via the tunnel.
    No agent path anymore (agent HTTP heartbeat removed since 0.9.1), but a lever for tests and diagnostics;
    disabled without router.heartbeat_token."""
    if not state.CFG.heartbeat_token:
        return web.json_response({"error": "heartbeat injection is off (router.heartbeat_token not set)"}, status=410)
    if request.headers.get("X-Router-Token") != state.CFG.heartbeat_token:
        return web.json_response({"error": "bad token"}, status=401)
    name = request.match_info["node"]
    node = state.NODES.get(name)
    if node is None:
        return web.json_response({"error": f"unknown node {name}"}, status=404)
    b = await read_json(request)
    if not isinstance(b, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    apply_heartbeat(node, b, time.time())
    return web.json_response(hb_ack(node))


def note_game_memory(node, b, now):
    """Agent >= 0.17.0 reports a running game and RAM as a fact (kay's rule: report state instead of guessing from
    side effects). Trigger 2026-10-04: Diablo IV + qwen3.8@196k -> RAM 94 %, free commit 1.8 GB, ~900 Hard
    Faults/s; the node stayed free because Diablo got little VRAM (no foreign VRAM) and the router didn't know RAM.
    If a block is missing, the agent doesn't report it (older, Linux without game detection): then old behavior remains."""
    g = b.get("game")
    if isinstance(g, dict) and g.get("supported", True):
        was = bool((node.game or {}).get("running"))
        node.game = {k: g.get(k) for k in ("running", "name", "via", "since", "procs", "gpu_dedicated_mib", "gpu_shared_mib", "gpu_pct")
                     if g.get(k) is not None}
        running = bool(g.get("running"))
        if running and node.game_since is None:
            node.game_since = now
        elif not running:
            node.game_since = None
            node.game_heavy, node.game_heavy_why = False, ""
        if running != was:
            log.info("node %s: game %s%s", node.name, "running" if running else "ended", f" ({g.get('name')})" if running else "")
            state.remember({"event": "game", "node": node.name, "running": running, "name": g.get("name") if running else None})
            state.MQTT_DIRTY.append(True)
    elif g is None:
        node.game, node.game_since = None, None
        node.game_heavy, node.game_heavy_why = False, ""
    m = b.get("memory")
    if not isinstance(m, dict):
        node.memory, node.mem_low_count, node.mem_pressure = None, 0, False
        return
    node.memory = {k: m.get(k) for k in ("ram_available_gib", "commit_free_gib", "ram_total_gib", "commit_limit_gib") if m.get(k) is not None}
    limit = state.CFG.memory_pressure["min_free_gib"]
    low = any(isinstance(m.get(k), (int, float)) and m[k] < limit for k in ("ram_available_gib", "commit_free_gib"))
    node.mem_low_count = node.mem_low_count + 1 if low else 0
    pressure = node.mem_low_count >= state.CFG.memory_pressure["polls"]
    if pressure != node.mem_pressure:
        node.mem_pressure = pressure
        log.info("node %s: memory pressure %s (RAM free %s GiB, commit free %s GiB, limit %s GiB)", node.name,
                 "begins" if pressure else "over", m.get("ram_available_gib"), m.get("commit_free_gib"), limit)
        state.remember({"event": "memory_pressure", "node": node.name, "on": pressure, "ram_available_gib": m.get("ram_available_gib"),
                        "commit_free_gib": m.get("commit_free_gib")})
        state.MQTT_DIRTY.append(True)


ROUTE_REASONS = ("", "unreachable", "unhealthy", "starting", "selftest_failed")


def route_health(raw):
    """Heartbeat field `routes` -> {name: {"up": bool, "reason": str}}. Only real booleans count as up; an unknown
    reason becomes "unhealthy" (agent is newer than router), an entry without mapping is dropped."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, r in raw.items():
        if not isinstance(name, str) or not isinstance(r, dict):
            continue
        up = r.get("up") is True
        reason = r.get("reason") if r.get("reason") in ROUTE_REASONS else "unhealthy"
        out[name] = {"up": up, "reason": "" if up else (reason or "unhealthy")}
    return out


def hb_ack(node):
    """Response to a heartbeat (HTTP and tunnel): router's verdict plus whether it respects this node's GPU guard."""
    return {"state": node.state, "busy_reason": node.busy_reason,
            "gpu_guard_ack": bool(state.CFG.gpu_guard["enabled"] and node.guard_policy)}


def apply_heartbeat(node, b, now):
    """Adopt GPU facts from a heartbeat, run state machine, learn baseline."""
    node.hb_ts = now
    if node.power == "asleep":   # a heartbeat is the agent running again, even if its resume notice was lost
        note_power(node, "awake", now, "heartbeat")
    sm = b.get("standby_models")   # Agent >= 0.25.0 with tunnel.upstream_fallback: the standby Ollama's model store
    if isinstance(sm, list):
        node.standby_models = {m for m in sm if isinstance(m, str) and m}
    fp = b.get("ollama_tls_sha256")
    if fp and node.tls and node.tls_mode == "auto" and str(fp).lower() != node.tls_fp:
        if node.set_fingerprint(fp):
            log.info("node %s: TLS fingerprint taken from the agent (%s...)", node.name, node.tls_fp[:16])
        else:
            log.warning("node %s: invalid TLS fingerprint: %r", node.name, fp)
    node.gpu_util = b.get("gpu_util_pct")
    total = b.get("vram_total_mib"); used = b.get("vram_used_mib"); free = b.get("vram_free_mib")
    node.vram_total_reported_gib = total / 1024 if total is not None else None
    node.vram_used_gib = used / 1024 if used is not None else None
    node.vram_free_gib = free / 1024 if free is not None else None
    op = b.get("ollama_proc_mib")
    node.ollama_proc_gib = op / 1024 if op is not None else None
    kv = b.get("children_vram_mib")   # Agent >= 0.13.0: {kind: MiB} without Ollama; if field is missing, no such children
    node.children_vram_gib = sum(v for v in kv.values() if isinstance(v, (int, float))) / 1024 if isinstance(kv, dict) else 0.0
    note_game_memory(node, b, now)   # Agent >= 0.17.0: game and RAM as fact
    node.routes = route_health(b.get("routes"))   # Tunnel routes next to Ollama (Reranker); older agents report none
    sens = b.get("sensors")   # Agent >= 0.6.0: temperature, power, throttling, GPU-Z values (passed through unchanged)
    node.sensors = sens if isinstance(sens, dict) else None
    if isinstance(b.get("update"), dict):   # Agent >= 0.8.0: Zwischenstand eines Update-Auftrags
        agentupdate.note_report(node, b["update"], now)
    if b.get("ollama_version"):   # Agent >= 0.12.0: Ollama version fresh from node (immediately visible after an Ollama update)
        ollamaupdate.note_version(node, str(b["ollama_version"]), now, source="agent")
    if isinstance(b.get("ollama_update"), dict):   # Agent >= 0.12.0: Zwischenstand eines Ollama-Update-Auftrags
        ollamaupdate.note_report(node, b["ollama_update"], now)
    g = b.get("gpu_guard")     # Agent >= 0.7.0: GPU guard (power limit, high-load tier, warnings) - only flag next to state
    if isinstance(g, dict):
        g = nodes.guard_english(g)   # Agents before 0.20.0 report in German
        prev = node.guard or {}
        node.guard, node.guard_ts = g, now
        if g.get("state") != prev.get("state") or bool(g.get("problem")) != bool(prev.get("problem")):
            reason = g.get("reason") or ", ".join(g.get("warnings") or [])
            log.info("node %s: GPU guard %s%s%s", node.name, g.get("state"), f" ({reason})" if reason else "",
                     f" Limit {g.get('limit_w')} W" if g.get("limit_w") is not None else "")
            state.remember({"event": "gpu_guard", "node": node.name, "state": g.get("state"), "limit_w": g.get("limit_w"),
                            "problem": bool(g.get("problem")), "reason": reason})
            state.MQTT_DIRTY.append(True)
    if node.vram_total_reported_gib and not node.vram_total_gib:
        node.vram_total_gib = node.vram_total_reported_gib
    # Learn baseline only if Ollama holds nothing in VRAM and no request is running: then "busy" is the pure
    # Desktop baseline (plus any game, which does not lower the 24h minimum). With a loaded model,
    # busy-minus-size_vram is unreliable (overflow into Shared Memory corrupts size_vram).
    # Learn only on an idle GPU: otherwise a running game drifts into the baseline as "Desktop" (gpu-desktop had
    # 19 of 23 hourly buckets at 18.2 GiB). The filter intentionally keys off gpu_util and NOT the busy state -
    # which is derived from the baseline; that would be circular: a node with high desktop usage would stay
    # busy forever and never learn that it is its normal state.
    if (node.vram_used_gib is not None and node.ollama_vram_claimed_gib() < 0.5 and node.inflight == 0
            and node.gpu_util is not None and node.gpu_util < node.busy_util_threshold()
            and node.ps_fresh(now)):   # only with fresh /api/ps data (otherwise a loaded model counts as desktop)
        node.learn_baseline(now, node.vram_used_gib - node.children_vram_gib)   # children are already subtracted by foreign_vram_gib
    evaluate(node, now)
