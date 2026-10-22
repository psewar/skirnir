"""Home Assistant: State view and MQTT discovery."""

import asyncio
import importlib.util
import json
import os
import threading
import time

from aiohttp import web

if importlib.util.find_spec("paho") is not None:   # Debian: python3-paho-mqtt (1.6); optional
    import paho.mqtt.client as mqtt
else:  # pragma: no cover
    mqtt = None

from . import scheduler, state
from .common import VERSION, log, read_env_value

NODE_ID = "skirnir_router"          # Discovery-Node-Id (homeassistant/<component>/<node_id>/<object_id>/config)
LEGACY_NODE_ID = "ollama_router"    # up to Router 0.1.9
LEGACY_BASE = "ollama-router"       # altes base_topic


def _usage_today_fields():
    from . import metrics   # late, to avoid import cycles
    t = metrics.usage_totals(metrics.usage_today())
    return {"requests_today": t["requests"], "tokens_today": t["prompt_tokens"] + t["completion_tokens"], "errors_today": t["errors"]}


def ha_snapshot(now=None):
    """Aggregated view for HA: nodes, models, role readiness (= could the router serve it NOW)."""
    now = now or time.time()
    online = [n for n in state.NODES.values() if n.state != "offline"]
    models = set()
    for n in online:
        models |= n.models
    roles = {}
    for _exposed, role in state.CFG.roles.items():
        pick = scheduler.choose(role, role["tiers"], None, now, mutate=False)   # View: no breaker switching from the HA view
        if pick:
            i, tier, ctx, node = pick
            roles[role["name"]] = {"ready": True, "node": node.name, "model": tier["model"], "tier": i, "num_ctx": ctx,
                                   "warm": node.is_loaded(tier["model"])}
        else:
            roles[role["name"]] = {"ready": False, "node": None, "model": None, "tier": None, "num_ctx": None, "warm": False}
    ready = sum(1 for r in roles.values() if r["ready"])
    routes = [d for d in state.DECISIONS if d.get("event") == "route" and now - d.get("t", 0) <= 300]
    last = next((d for d in reversed(state.DECISIONS) if d.get("event") == "route"), None)
    nodes = {}
    for n in state.NODES.values():
        nodes[n.name] = {"state": n.state, "busy_reason": n.busy_reason, "gpu": n.gpu, "gpu_util": n.gpu_util if n.gpu_known(now) else None,
                         "vram_free_gib": round(n.vram_free_gib, 2) if (n.gpu_known(now) and n.vram_free_gib is not None) else None,
                         "vram_total_gib": n.vram_total_gib, "loaded": sorted(n.loaded), "models": sorted(n.models),
                         "inflight": n.inflight, "agent": bool(n.gpu_known(now)), "gpu_guard": n.guard_view(now),
                         "ollama_version": n.ollama_version,
                         # Agent >= 0.17.0: game and main memory (None = agent does not report)
                         "game": (n.game or {}).get("name") if (n.game or {}).get("running") else None,
                         "game_reported": n.game is not None, "memory_pressure": n.mem_pressure,
                         "ram_available_gib": (n.memory or {}).get("ram_available_gib"),
                         "commit_free_gib": (n.memory or {}).get("commit_free_gib"),
                         # Router 0.9.2, slowness watch: model partly in system RAM (fact from /api/ps) and the rates of the
                         # last warm request large enough to measure (None until there was one)
                         "ram_spill_gib": round(sum(n.ram_spill.values()), 2),
                         "ram_spill_models": {m: round(g, 2) for m, g in sorted(n.ram_spill.items())},
                         "gen_tps": (n.last_gen or {}).get("tps"), "gen_tps_model": (n.last_gen or {}).get("model"),
                         "prompt_tps": (n.last_prompt or {}).get("tps"), "prompt_tps_model": (n.last_prompt or {}).get("model")}
    pending = [e["name"] for e in (state.REG.nodes.values() if state.REG else []) if e.get("state") == "pending"]
    problems = []
    if not online:
        problems.append("kein Knoten online")
    # A role that is not servable only because its nodes are currently busy (game) is not a
    # router problem, but intended (a game would otherwise trigger the "role large not servable" alarm).
    # It remains a problem if no online node could serve the role at all (model missing, VRAM too small).
    limited = []
    for _exposed, role in state.CFG.roles.items():
        r = roles[role["name"]]
        if r["ready"]:
            continue
        if scheduler.role_possible_when_free(role):
            r["limited_by_busy"] = True
            limited.append(role["name"])
        else:
            problems.append(f"Rolle {role['name']} nicht bedienbar")
    # Agent failure: node reachable, but the heartbeat agent, which has reported before, is silent.
    # Without an agent, there is no busy detection -> a game would no longer evict the large model.
    for n in online:
        if n.hb_ts and now - n.hb_ts > state.CFG.agent_missing_s:
            problems.append(f"Knoten {n.name}: kein Agent-Heartbeat seit {int((now - n.hb_ts) // 60)} min")
    # Router 0.9.2: a loaded model that Ollama had to place partly in system RAM runs there on the CPU, every request is many
    # times slower. Nothing fails, so without this fact it only shows as slow answers (2026-10-22: a day unnoticed).
    if state.CFG.ram_spill_problem_gib > 0:
        for n in online:
            for m, g in sorted(n.ram_spill.items()):
                if g >= state.CFG.ram_spill_problem_gib:
                    problems.append(f"Knoten {n.name}: {m} liegt zu {g:.1f} GiB im Arbeitsspeicher (rechnet langsam auf der CPU)")
    # GPU guard (Agent >= 0.7.0): unavailable (limit not settable), deselected (operator decision: counts as a problem),
    # voltage/temperature/hardware warning. Only if the router should consider this node's status.
    if state.CFG.gpu_guard["enabled"]:
        for n in online:
            g = n.guard or {}
            if n.guard_policy and g.get("problem") and n.guard_state(now) is not None:
                reason = g.get("reason") or ", ".join(g.get("warnings") or []) or "?"
                problems.append(f"Knoten {n.name}: GPU-Schutz {g.get('state')} ({reason})")
    from . import agentupdate, ollamaupdate
    problems += agentupdate.problems(now)   # failed or hanging agent updates
    problems += ollamaupdate.problems(now)  # failed or hanging Ollama updates, persistently failing version check
    ollama_pending = ollamaupdate.pending_nodes(now)
    from . import cloud
    problems += cloud.budget_problems()   # Stage 5: budget warning/exhaustion as HA problem
    cloud_view = cloud.budget_view()
    return {
        "cloud": cloud_view, "cloud_spend_month_chf": round(sum(v["spend_month_chf"] for v in cloud_view.values()), 4),
        "cloud_budget_month_chf": round(sum(v["budget_month_chf"] for v in cloud_view.values()), 2),
        "nodes_total": len(state.NODES), "nodes_online": len(online),
        "nodes_busy": sum(1 for n in online if n.state == "busy"),
        "models_available": len(models), "models": sorted(models),
        "roles_ready": ready, "roles_total": len(roles), "roles": roles, "roles_limited": limited,
        "nodes_pending": len(pending), "pending": pending,
        "problem": bool(problems), "problems": problems,
        "requests_5min": len(routes),
        **{k: v for k, v in _usage_today_fields().items()},
        "last_route": (f"{last['role']} -> {last['node']} {last['model']} (tier {last['tier']}, ctx {last['ctx']})" if last else "-"),
        "last_route_ts": last["ts"] if last else None,
        "truncations": state.TRUNCATIONS["count"], "last_truncation": state.TRUNCATIONS["last"],
        "ollama_latest": ollamaupdate.LATEST.get("version"), "ollama_updates_pending": len(ollama_pending), "ollama_updates": ollama_pending,
        "nodes": nodes, "version": VERSION, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


async def handle_ha(request):
    return web.json_response(ha_snapshot())


class HAPublisher:
    """Registers the router as device 'Skirnir' with HA (MQTT discovery) and publishes the state.
    LWT: <base>/status = offline -> all entities in HA 'unavailable' as soon as the router dies.
    Names since 2026-09-25 (Router 0.2.0): base_topic `skirnir-router`, Discovery node ID `skirnir_router`, unique_ids
    `skirnir_router_<oid>`. Since 0.9.0, `default_entity_id` sets the Entity-ID in English (`<domain>.skirnir_<entity>`);
    previously HA derived it from the German display names (design/english-names.md). Existing installations keep their
    IDs until the registry is renamed. The retained topics of the old names (`ollama-router`, `ollama_router`) are cleaned
    up by sweep_orphans on the first connect."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.get("base_topic", "skirnir-router")
        self.legacy_cfg = set()   # retained discovery configs under the old node ID name (one-time cleanup)
        self.legacy_state = set()   # retained node states under the old base topic
        self.prefix = cfg.get("discovery_prefix", "homeassistant")
        self.interval = float(cfg.get("interval_s", 10))
        self.client = None
        self.connected = False
        self.node_ents = {}                          # (component, oid) -> node name, what is currently in HA
        self.seen = {"cfg": set(), "state": set()}   # retained topics replayed on connect
        self.seen_lock = threading.Lock()            # _on_message runs in the paho network thread
        self.sweep_at = 0.0                          # timestamp for the ghost reconciliation
        self.announce = False                        # after (re-)connect: discovery + online + state, from loop()

    def start(self):
        if mqtt is None:
            log.warning("paho-mqtt missing -> no HA connection")
            return
        key = self.cfg.get("password_env", "MQTT_PASSWORD")
        pw = read_env_value(self.cfg["password_file"], key) if self.cfg.get("password_file") else None   # raw, as registry.read_mqtt_password
        pw = pw or os.environ.get(key) or self.cfg.get("password")
        c = mqtt.Client(client_id=self.cfg.get("client_id", "skirnir-router"), clean_session=True)
        if self.cfg.get("username"):
            c.username_pw_set(self.cfg["username"], pw)
        if self.cfg.get("tls"):
            c.tls_set()   # System-CA; Broker-Zertifikat = LE ha.example.net, deshalb host per Name
        c.will_set(f"{self.base}/status", "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.reconnect_delay_set(2, 60)
        self.client = c
        try:
            c.connect_async(self.cfg["host"], int(self.cfg.get("port", 1883)), keepalive=30)
            c.loop_start()
        except Exception as e:  # noqa: BLE001  # exception-ok: MQTT broker boundary; paho reconnects by itself, HA is optional
            log.error("MQTT connect failed: %s", e)

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect rc=%s", rc)
            return
        log.info("MQTT connected to %s", self.cfg["host"])
        # The broker replays its own retained topics; from this we detect ghosts (nodes that no
        # longer exist) and clean them up in sweep_orphans().
        with self.seen_lock:
            self.seen = {"cfg": set(), "state": set()}
        client.on_message = self._on_message
        client.subscribe([(f"{self.prefix}/+/{NODE_ID}/+/config", 1), (f"{self.base}/node/+", 1),
                          (f"{self.prefix}/+/{LEGACY_NODE_ID}/+/config", 1), (f"{LEGACY_BASE}/node/+", 1)])
        self.sweep_at = time.time() + 5
        # This callback runs in the paho network thread. Discovery and state need the scheduler and write to the
        # decision log (loop state) - so just mark it, loop() publishes (Review 2026-09-25).
        self.announce = True
        self.connected = True

    def _on_disconnect(self, client, userdata, rc, *a):
        self.connected = False
        log.warning("MQTT disconnected (rc=%s)", rc)

    def _on_message(self, client, userdata, msg):
        """Just record which retained topics exist - otherwise the router listens to nothing."""
        if not msg.payload:
            return
        p = msg.topic.split("/")
        with self.seen_lock:
            if len(p) == 5 and p[0] == self.prefix and p[2] == NODE_ID and p[4] == "config":
                self.seen["cfg"].add((p[1], p[3]))
            elif len(p) == 5 and p[0] == self.prefix and p[2] == LEGACY_NODE_ID and p[4] == "config":
                self.legacy_cfg.add(msg.topic)
            elif len(p) == 3 and p[0] == self.base and p[1] == "node":
                self.seen["state"].add(p[2])
            elif len(p) == 3 and p[0] == LEGACY_BASE and p[1] == "node":
                self.legacy_state.add(msg.topic)

    def _dev(self):
        # Device "Skirnir" (since 2026-09-16; 2026-09-09..16 "Huginn"); identifiers/unique_ids since 2026-09-25 skirnir-router.
        return {"identifiers": ["skirnir-router"], "name": "Skirnir", "manufacturer": "Skirnir",
                "model": "skirnir-router", "sw_version": VERSION,
                "configuration_url": state.CFG.public_url or None}

    def _ent(self, component, oid, name, entity=None, **extra):
        """Register an entity. `oid` determines unique_id and discovery topic and remains fixed for existing entities
        (a new unique_id would create a second entity and leave history and automations on an orphan);
        `entity` is the English name of the Entity-ID, if it differs."""
        payload = {"name": name, "unique_id": f"skirnir_router_{oid}", "default_entity_id": f"{component}.skirnir_{entity or oid}",
                   "state_topic": f"{self.base}/state", "device": self._dev(), **extra}
        if "availability" not in payload:   # HA forbids availability and availability_topic together
            payload["availability_topic"] = f"{self.base}/status"
        self.client.publish(f"{self.prefix}/{component}/{NODE_ID}/{oid}/config", json.dumps(payload), qos=1, retain=True)

    def _drop_ent(self, component, oid):
        self.client.publish(f"{self.prefix}/{component}/{NODE_ID}/{oid}/config", "", qos=1, retain=True)

    def _node_entities(self, name):
        """Die drei Entitaeten eines Knotens als (component, oid, Anzeigename, extra)."""
        oid = "".join(ch if ch.isalnum() else "_" for ch in name.lower())
        nt = f"{self.base}/node/{name}"

        def numeric(field):
            """A numeric value that may be missing: an empty template makes HA discard the update, and
            as long as the node does not provide GPU data, the entity is unavailable instead of wrong.
            A text like 'unknown' in the State would be a Value-Error here, because state_class expects a number."""
            return dict(
                state_topic=nt,
                value_template=f"{{{{ value_json.{field} if value_json.{field} is not none else '' }}}}",
                availability=[
                    {"topic": f"{self.base}/status"},
                    {"topic": nt, "payload_available": "ok", "payload_not_available": "no",
                     "value_template": f"{{{{ 'ok' if value_json.{field} is not none else 'no' }}}}"},
                ],
                availability_mode="all",
                state_class="measurement",
            )

        return [
            ("sensor", f"node_{oid}_state", f"{name} Zustand",
             dict(state_topic=nt, value_template="{{ value_json.state }}", icon="mdi:desktop-tower", json_attributes_topic=nt)),
            ("sensor", f"node_{oid}_gpu_util", f"{name} GPU-Auslastung",
             dict(unit_of_measurement="%", icon="mdi:expansion-card", **numeric("gpu_util"))),
            ("sensor", f"node_{oid}_vram_free", f"{name} VRAM frei",
             dict(unit_of_measurement="GiB", device_class="data_size", icon="mdi:memory", **numeric("vram_free_gib"))),
            # Agent >= 0.17.0: game and main memory as fact (Router 0.7.0)
            ("sensor", f"node_{oid}_game", f"{name} Spiel",
             dict(state_topic=nt, value_template="{{ value_json.game if value_json.game else 'none' }}", icon="mdi:gamepad-variant")),
            ("sensor", f"node_{oid}_ram_free", f"{name} RAM frei",
             dict(unit_of_measurement="GiB", device_class="data_size", icon="mdi:memory", **numeric("ram_available_gib"))),
            ("sensor", f"node_{oid}_commit_free", f"{name} Commit frei",
             dict(unit_of_measurement="GiB", device_class="data_size", icon="mdi:memory", **numeric("commit_free_gib"))),
            # Router 0.9.2, slowness watch: the RAM fact as a problem sensor, and the rates of the last measurable request
            # per node as raw values for a baseline in HA (statistics helper; low rate = slow)
            ("binary_sensor", f"node_{oid}_ram_spill", f"{name} Modell teilweise im RAM",
             dict(state_topic=nt, value_template="{{ 'ON' if (value_json.ram_spill_gib or 0) > 0 else 'OFF' }}",
                  device_class="problem", icon="mdi:memory-arrow-down", json_attributes_topic=nt,
                  json_attributes_template="{{ {'gib': value_json.ram_spill_gib, 'models': value_json.ram_spill_models} | tojson }}")),
            ("sensor", f"node_{oid}_gen_tps", f"{name} Erzeugung letzte Anfrage",
             dict(unit_of_measurement="tok/s", icon="mdi:speedometer", json_attributes_topic=nt,
                  json_attributes_template="{{ {'model': value_json.gen_tps_model} | tojson }}", **numeric("gen_tps"))),
            ("sensor", f"node_{oid}_prompt_tps", f"{name} Prompt-Verarbeitung letzte Anfrage",
             dict(unit_of_measurement="tok/s", icon="mdi:speedometer-slow", json_attributes_topic=nt,
                  json_attributes_template="{{ {'model': value_json.prompt_tps_model} | tojson }}", **numeric("prompt_tps"))),
        ]

    def sync_node_discovery(self):
        """Reconcile node entities with the current inventory: create new ones, delete vanished ones from HA.
        Without this, every test node remains a ghost, because discovery configs are retained."""
        if not self.client or not self.connected:
            return
        want, names = {}, {n.name for n in state.NODES.values()}
        for name in names:
            for comp, oid, label, extra in self._node_entities(name):
                want[(comp, oid)] = (name, label, extra)
        if set(want) == set(self.node_ents):
            return
        for key, (_, label, extra) in want.items():
            if key not in self.node_ents:
                self._ent(key[0], key[1], label, **extra)
        for key in set(self.node_ents) - set(want):
            self._drop_ent(*key)
        for name in {v for k, v in self.node_ents.items() if k not in want} - names:
            self.client.publish(f"{self.base}/node/{name}", "", qos=0, retain=True)   # also do not leave the state behind
        self.node_ents = {k: v[0] for k, v in want.items()}

    def sweep_orphans(self):
        """Compare retained topics collected on connect with the inventory and delete orphans.
        Catches nodes that vanished while the router was down (e.g. a console test)."""
        if not self.client or not self.connected:
            return
        # End subscription: it was only needed for the replayed retained topics, otherwise from now on
        # every own state message would come back as an echo.
        self.client.unsubscribe([f"{self.prefix}/+/{NODE_ID}/+/config", f"{self.base}/node/+",
                                 f"{self.prefix}/+/{LEGACY_NODE_ID}/+/config", f"{LEGACY_BASE}/node/+"])
        # Rename 2026-09-25: delete all retained data under old names (Discovery configs, states, status). One-time -
        # afterwards, nothing remains retained under old topics; collections stay empty.
        if self.legacy_cfg or self.legacy_state:
            for t in sorted(self.legacy_cfg | self.legacy_state) + [f"{LEGACY_BASE}/state", f"{LEGACY_BASE}/status"]:
                self.client.publish(t, "", qos=1, retain=True)
            log.info("MQTT: %d retained topics of the old names (%s, %s) deleted", len(self.legacy_cfg) + len(self.legacy_state), LEGACY_BASE, LEGACY_NODE_ID)
            self.legacy_cfg, self.legacy_state = set(), set()
        with self.seen_lock:
            seen_cfg, seen_state = self.seen["cfg"], self.seen["state"]
            self.seen = {"cfg": set(), "state": set()}
        names = {n.name for n in state.NODES.values()}
        gone = 0
        for comp, oid in seen_cfg - set(self.node_ents):
            if not oid.startswith("node_"):
                continue                      # never touch the fixed router entities
            self._drop_ent(comp, oid)
            gone += 1
        for name in seen_state - names:
            self.client.publish(f"{self.base}/node/{name}", "", qos=0, retain=True)
            gone += 1
        if gone:
            log.info("HA: %d orphaned topics removed (nodes no longer exist)", gone)

    def publish_discovery(self):
        ent, st = self._ent, f"{self.base}/state"
        ent("sensor", "nodes_online", "Knoten online", value_template="{{ value_json.nodes_online }}", icon="mdi:server-network",
            state_class="measurement", json_attributes_topic=st, json_attributes_template="{{ {'total': value_json.nodes_total, 'nodes': value_json.nodes} | tojson }}")
        ent("sensor", "nodes_busy", "Knoten belegt", value_template="{{ value_json.nodes_busy }}", icon="mdi:controller", state_class="measurement")
        ent("sensor", "models_available", "Modelle verfuegbar", value_template="{{ value_json.models_available }}", icon="mdi:brain",
            state_class="measurement", json_attributes_topic=st, json_attributes_template="{{ {'models': value_json.models} | tojson }}")
        ent("sensor", "roles_ready", "Rollen bereit", value_template="{{ value_json.roles_ready }}", icon="mdi:account-switch",
            state_class="measurement", json_attributes_topic=st, json_attributes_template="{{ {'total': value_json.roles_total, 'roles': value_json.roles, 'limited_by_busy': value_json.roles_limited} | tojson }}")
        ent("binary_sensor", "problem", "Problem", value_template="{{ 'ON' if value_json.problem else 'OFF' }}", device_class="problem",
            json_attributes_topic=st, json_attributes_template="{{ {'problems': value_json.problems} | tojson }}")
        ent("sensor", "nodes_pending", "Knoten wartet auf Freigabe", value_template="{{ value_json.nodes_pending }}", icon="mdi:account-clock",
            state_class="measurement", json_attributes_topic=st, json_attributes_template="{{ {'pending': value_json.pending} | tojson }}")
        ent("binary_sensor", "pending", "Neuer Knoten", value_template="{{ 'ON' if value_json.nodes_pending else 'OFF' }}", icon="mdi:account-plus")
        ent("sensor", "requests_5min", "Anfragen (5 min)", value_template="{{ value_json.requests_5min }}", icon="mdi:swap-horizontal", state_class="measurement")
        ent("sensor", "requests_today", "Anfragen heute", value_template="{{ value_json.requests_today }}", icon="mdi:counter", state_class="total")
        ent("sensor", "cloud_spend", "Cloud-Kosten Monat", value_template="{{ value_json.cloud_spend_month_chf }}", icon="mdi:cloud-outline", unit_of_measurement="CHF",
            state_class="total", json_attributes_topic=st, json_attributes_template="{{ {'budget_chf': value_json.cloud_budget_month_chf, 'providers': value_json.cloud} | tojson }}")
        ent("sensor", "tokens_today", "Tokens heute", value_template="{{ value_json.tokens_today }}", icon="mdi:text-long", state_class="total",
            json_attributes_topic=st, json_attributes_template="{{ {'errors_today': value_json.errors_today} | tojson }}")
        ent("sensor", "last_route", "Letzte Zuweisung", value_template="{{ value_json.last_route }}", icon="mdi:routes",
            json_attributes_topic=st, json_attributes_template="{{ {'ts': value_json.last_route_ts} | tojson }}")
        # Silent truncations (context_check.py): counter since router start + event per confirmed truncation. oid (unique_id,
        # Discovery topic) keeps the 2026 name so HA retains history and automations; the Entity-ID is English.
        ent("sensor", "kuerzungen", "Gekürzte Anfragen", entity="truncations", value_template="{{ value_json.truncations }}",
            icon="mdi:content-cut", state_class="total_increasing", json_attributes_topic=st,
            json_attributes_template="{{ {'last': value_json.last_truncation} | tojson }}")
        ent("event", "kuerzung", "Anfrage gekürzt", entity="truncation", state_topic=f"{self.base}/truncation",
            event_types=["context_truncated"], icon="mdi:content-cut")
        # Ollama update (0.3.0): nodes missing the latest Ollama version; attributes: latest version, running/available per node
        ent("sensor", "ollama_updates", "Ollama-Updates ausstehend", value_template="{{ value_json.ollama_updates_pending }}", icon="mdi:update",
            state_class="measurement", json_attributes_topic=st,
            json_attributes_template="{{ {'latest': value_json.ollama_latest, 'nodes': value_json.ollama_updates} | tojson }}")
        self.sync_node_discovery()

    def publish_state(self):
        if not self.client or not self.connected:
            return
        self.sync_node_discovery()   # a newly released node should not wait until the next Connect
        snap = ha_snapshot()
        self.client.publish(f"{self.base}/state", json.dumps(snap), qos=0, retain=True)
        for name, n in snap["nodes"].items():
            self.client.publish(f"{self.base}/node/{name}", json.dumps(n), qos=0, retain=True)

    async def loop(self):
        last = 0.0
        while True:
            await asyncio.sleep(1)
            if self.announce and self.client and self.connected:
                self.announce = False
                try:
                    self.node_ents = {}
                    self.publish_discovery()
                    self.client.publish(f"{self.base}/status", "online", qos=1, retain=True)
                    self.publish_state()
                    last = time.time()
                except Exception as e:  # noqa: BLE001  # exception-ok: MQTT boundary in a background loop; the next connect announces again
                    log.warning("MQTT announce: %s", e)
            if self.sweep_at and time.time() >= self.sweep_at:
                self.sweep_at = 0.0
                try:
                    self.sweep_orphans()
                except Exception as e:  # noqa: BLE001  # exception-ok: MQTT boundary in a background loop; the sweep is best effort
                    log.warning("MQTT sweep: %s", e)
            if state.MQTT_EVENTS and self.client and self.connected:
                events, state.MQTT_EVENTS[:] = list(state.MQTT_EVENTS), []
                for e in events:
                    try:
                        self.client.publish(f"{self.base}/truncation", json.dumps(e), qos=1, retain=False)
                    except Exception as ex:  # noqa: BLE001  # exception-ok: MQTT broker boundary; one lost event, the loop goes on
                        log.warning("MQTT event: %s", ex)
            if state.MQTT_DIRTY or time.time() - last >= self.interval:
                state.MQTT_DIRTY.clear()
                try:
                    self.publish_state()
                except Exception as e:  # noqa: BLE001  # exception-ok: MQTT boundary in a background loop; the next interval publishes again
                    log.warning("MQTT publish: %s", e)
                last = time.time()

    def stop(self):
        if self.client:
            try:
                self.client.publish(f"{self.base}/status", "offline", qos=1, retain=True).wait_for_publish(2)
                self.client.loop_stop()
                self.client.disconnect()
            except Exception as e:  # noqa: BLE001  # exception-ok: shutdown at the MQTT boundary; the broker may already be gone
                log.debug("MQTT stop: %s", e)
