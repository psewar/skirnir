"""Home Assistant: Zustandsbild und MQTT-Discovery."""

import asyncio
import json
import os
import threading
import time

from aiohttp import web

try:
    import paho.mqtt.client as mqtt  # Debian: python3-paho-mqtt (1.6); optional
except ImportError:  # pragma: no cover
    mqtt = None

from . import scheduler, state
from .common import VERSION, log, read_env_value

NODE_ID = "skirnir_router"          # Discovery-Node-Id (homeassistant/<component>/<node_id>/<object_id>/config)
LEGACY_NODE_ID = "ollama_router"    # bis Router 0.1.9
LEGACY_BASE = "ollama-router"       # altes base_topic


def _usage_today_fields():
    try:
        from . import metrics
        t = metrics.usage_totals(metrics.usage_today())
        return {"requests_today": t["requests"], "tokens_today": t["prompt_tokens"] + t["completion_tokens"],
                "errors_today": t["errors"]}
    except Exception:  # noqa: BLE001
        return {"requests_today": 0, "tokens_today": 0, "errors_today": 0}


def ha_snapshot(now=None):
    """Aggregierte Sicht fuer HA: Knoten, Modelle, Rollen-Bereitschaft (= wuerde der Router JETZT bedienen koennen)."""
    now = now or time.time()
    online = [n for n in state.NODES.values() if n.state != "offline"]
    models = set()
    for n in online:
        models |= n.models
    roles = {}
    for _exposed, role in state.CFG.roles.items():
        pick = scheduler.choose(role, role["tiers"], None, now, mutate=False)   # Sicht: kein Breaker-Wechsel aus dem HA-Bild
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
                         "inflight": n.inflight, "agent": bool(n.gpu_known(now)), "gpu_guard": n.guard_view(now)}
    pending = [e["name"] for e in (state.REG.nodes.values() if state.REG else []) if e.get("state") == "pending"]
    problems = []
    if not online:
        problems.append("kein Knoten online")
    # Eine Rolle, die nur deshalb nicht bedienbar ist, weil ihre Knoten gerade belegt sind (Spiel), ist kein
    # Router-Problem, sondern gewollt (ein Spiel loeste sonst den Alarm "Rolle gross nicht bedienbar" aus).
    # Problem bleibt es, wenn kein online-Knoten die Rolle ueberhaupt bedienen koennte (Modell fehlt, VRAM zu klein).
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
    # Agent-Ausfall: Knoten erreichbar, aber der Heartbeat-Agent, der schon einmal gemeldet hat, schweigt.
    # Ohne Agent gibt es keine busy-Erkennung -> ein Spiel wuerde das grosse Modell nicht mehr verdraengen.
    for n in online:
        if n.hb_ts and now - n.hb_ts > state.CFG.agent_missing_s:
            problems.append(f"Knoten {n.name}: kein Agent-Heartbeat seit {int((now - n.hb_ts) // 60)} min")
    # GPU-Schutz (Agent >= 0.7.0): unverfuegbar (Limit nicht setzbar), abgewaehlt (Betreiber-Entscheid: zaehlt als Problem),
    # Spannungs-/Temperatur-/Hardware-Warnung. Nur wenn der Router den Status dieses Knotens beachten soll.
    if state.CFG.gpu_guard["enabled"]:
        for n in online:
            g = n.guard or {}
            if n.guard_policy and g.get("problem") and n.guard_state(now) is not None:
                reason = g.get("grund") or ", ".join(g.get("warnungen") or []) or "?"
                problems.append(f"Knoten {n.name}: GPU-Schutz {g.get('state')} ({reason})")
    from . import agentupdate
    problems += agentupdate.problems(now)   # fehlgeschlagene oder haengende Agent-Updates
    from . import cloud
    problems += cloud.budget_problems()   # Stufe 5: Budgetwarnung/-erschoepfung als HA-Problem
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
        "kuerzungen": state.KUERZUNGEN["anzahl"], "letzte_kuerzung": state.KUERZUNGEN["letzte"],
        "nodes": nodes, "version": VERSION, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


async def handle_ha(request):
    return web.json_response(ha_snapshot())


class HAPublisher:
    """Meldet den Router als Geraet 'Skirnir' bei HA an (MQTT-Discovery) und publiziert den Zustand.
    LWT: <base>/status = offline -> alle Entitaeten in HA 'unavailable', sobald der Router stirbt.
    Namen seit 2026-09-25 (Router 0.2.0): base_topic `skirnir-router`, Discovery-Node-Id `skirnir_router`, unique_ids
    `skirnir_router_<oid>`, object_ids `skirnir_<oid>` (Entity-IDs sensor.skirnir_*). Die retained Themen der alten Namen
    (`ollama-router`, `ollama_router`) raeumt sweep_orphans beim ersten Connect weg; HA-seitige Entity-ID-Uebernahme macht
    das Betriebsskript (Register-Update ueber WebSocket)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.get("base_topic", "skirnir-router")
        self.legacy_cfg = set()   # retained Discovery-Configs unter dem alten Node-Id-Namen (einmalige Aufraeumung)
        self.legacy_state = set()   # retained Knotenzustaende unter dem alten Basisthema
        self.prefix = cfg.get("discovery_prefix", "homeassistant")
        self.interval = float(cfg.get("interval_s", 10))
        self.client = None
        self.connected = False
        self.node_ents = {}                          # (component, oid) -> Knotenname, was gerade in HA steht
        self.seen = {"cfg": set(), "state": set()}   # beim Connect zurueckgespielte retained Themen
        self.seen_lock = threading.Lock()            # _on_message laeuft im Netzwerk-Thread von paho
        self.sweep_at = 0.0                          # Zeitpunkt fuer den Karteileichen-Abgleich
        self.announce = False                        # nach (Re-)Connect: Discovery + online + Zustand, aus loop() heraus

    def start(self):
        if mqtt is None:
            log.warning("paho-mqtt fehlt -> keine HA-Anbindung")
            return
        key = self.cfg.get("password_env", "MQTT_PASSWORD")
        pw = read_env_value(self.cfg["password_file"], key) if self.cfg.get("password_file") else None   # roh, wie registry.read_mqtt_password
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
        except Exception as e:  # noqa: BLE001
            log.error("MQTT connect failed: %s", e)

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect rc=%s", rc)
            return
        log.info("MQTT verbunden mit %s", self.cfg["host"])
        # Der Broker spielt die eigenen retained Themen zurueck; daraus erkennen wir Karteileichen (Knoten, die es
        # nicht mehr gibt) und raeumen sie in sweep_orphans() auf.
        with self.seen_lock:
            self.seen = {"cfg": set(), "state": set()}
        client.on_message = self._on_message
        client.subscribe([(f"{self.prefix}/+/{NODE_ID}/+/config", 1), (f"{self.base}/node/+", 1),
                          (f"{self.prefix}/+/{LEGACY_NODE_ID}/+/config", 1), (f"{LEGACY_BASE}/node/+", 1)])
        self.sweep_at = time.time() + 5
        # Dieser Callback laeuft im Netzwerk-Thread von paho. Discovery und Zustand brauchen den Scheduler und schreiben ins
        # Entscheidungsprotokoll (Loop-Zustand) - deshalb nur merken, loop() publiziert (Review 2026-09-25).
        self.announce = True
        self.connected = True

    def _on_disconnect(self, client, userdata, rc, *a):
        self.connected = False
        log.warning("MQTT getrennt (rc=%s)", rc)

    def _on_message(self, client, userdata, msg):
        """Nur mitschreiben, welche retained Themen es gibt - der Router hoert sonst auf nichts."""
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
        # Geraet "Skirnir" (seit 2026-09-16; 2026-09-09..16 "Huginn"); identifiers/unique_ids seit 2026-09-25 skirnir-router.
        return {"identifiers": ["skirnir-router"], "name": "Skirnir", "manufacturer": "Skirnir",
                "model": "skirnir-router", "sw_version": VERSION,
                "configuration_url": state.CFG.public_url or None}

    def _ent(self, component, oid, name, **extra):
        payload = {"name": name, "unique_id": f"skirnir_router_{oid}", "object_id": f"skirnir_{oid}",
                   "state_topic": f"{self.base}/state", "device": self._dev(), **extra}
        if "availability" not in payload:   # HA verbietet availability und availability_topic gemeinsam
            payload["availability_topic"] = f"{self.base}/status"
        self.client.publish(f"{self.prefix}/{component}/{NODE_ID}/{oid}/config", json.dumps(payload), qos=1, retain=True)

    def _drop_ent(self, component, oid):
        self.client.publish(f"{self.prefix}/{component}/{NODE_ID}/{oid}/config", "", qos=1, retain=True)

    def _node_entities(self, name):
        """Die drei Entitaeten eines Knotens als (component, oid, Anzeigename, extra)."""
        oid = "".join(ch if ch.isalnum() else "_" for ch in name.lower())
        nt = f"{self.base}/node/{name}"

        def numeric(field):
            """Ein Zahlenwert, der fehlen kann: leeres Template laesst HA das Update verwerfen, und
            solange der Knoten keine GPU-Daten liefert, ist die Entitaet unavailable statt falsch.
            Ein Text wie 'unknown' im State waere hier ein Value-Error, weil state_class eine Zahl will."""
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
        ]

    def sync_node_discovery(self):
        """Knoten-Entitaeten dem aktuellen Bestand nachfuehren: neue anlegen, verschwundene aus HA loeschen.
        Ohne das bleibt jeder Testknoten als Karteileiche stehen, weil Discovery-Configs retained sind."""
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
            self.client.publish(f"{self.base}/node/{name}", "", qos=0, retain=True)   # auch den Zustand nicht liegen lassen
        self.node_ents = {k: v[0] for k, v in want.items()}

    def sweep_orphans(self):
        """Beim Connect eingesammelte retained Themen mit dem Bestand vergleichen und Verwaistes loeschen.
        Faengt Knoten, die verschwanden, waehrend der Router aus war (z. B. ein Konsolentest)."""
        if not self.client or not self.connected:
            return
        # Abo beenden: gebraucht wurde es nur fuer die zurueckgespielten retained Themen, sonst kaeme ab jetzt
        # jede eigene Zustandsmeldung als Echo zurueck.
        self.client.unsubscribe([f"{self.prefix}/+/{NODE_ID}/+/config", f"{self.base}/node/+",
                                 f"{self.prefix}/+/{LEGACY_NODE_ID}/+/config", f"{LEGACY_BASE}/node/+"])
        # Umbenennung 2026-09-25: alles Retained unter den alten Namen loeschen (Discovery-Configs, Zustaende, Status). Einmalig -
        # danach ist unter den alten Themen nichts mehr retained, die Mengen bleiben leer.
        if self.legacy_cfg or self.legacy_state:
            for t in sorted(self.legacy_cfg | self.legacy_state) + [f"{LEGACY_BASE}/state", f"{LEGACY_BASE}/status"]:
                self.client.publish(t, "", qos=1, retain=True)
            log.info("MQTT: %d retained Themen der alten Namen (%s, %s) geloescht", len(self.legacy_cfg) + len(self.legacy_state), LEGACY_BASE, LEGACY_NODE_ID)
            self.legacy_cfg, self.legacy_state = set(), set()
        with self.seen_lock:
            seen_cfg, seen_state = self.seen["cfg"], self.seen["state"]
            self.seen = {"cfg": set(), "state": set()}
        names = {n.name for n in state.NODES.values()}
        gone = 0
        for comp, oid in seen_cfg - set(self.node_ents):
            if not oid.startswith("node_"):
                continue                      # die festen Router-Entitaeten nie anfassen
            self._drop_ent(comp, oid)
            gone += 1
        for name in seen_state - names:
            self.client.publish(f"{self.base}/node/{name}", "", qos=0, retain=True)
            gone += 1
        if gone:
            log.info("HA: %d verwaiste Themen entfernt (Knoten gibt es nicht mehr)", gone)

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
        # Stille Kuerzungen (kontextpruefung.py): Zaehler seit Routerstart + Ereignis je bestaetigter Kuerzung
        ent("sensor", "kuerzungen", "Gekürzte Anfragen", value_template="{{ value_json.kuerzungen }}", icon="mdi:content-cut",
            state_class="total_increasing", json_attributes_topic=st,
            json_attributes_template="{{ {'letzte': value_json.letzte_kuerzung} | tojson }}")
        ent("event", "kuerzung", "Anfrage gekürzt", state_topic=f"{self.base}/kuerzung", event_types=["kontext_gekuerzt"],
            icon="mdi:content-cut")
        self.sync_node_discovery()

    def publish_state(self):
        if not self.client or not self.connected:
            return
        self.sync_node_discovery()   # ein neu freigegebener Knoten soll nicht bis zum naechsten Connect warten
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
                except Exception as e:  # noqa: BLE001
                    log.warning("MQTT Anmeldung: %s", e)
            if self.sweep_at and time.time() >= self.sweep_at:
                self.sweep_at = 0.0
                try:
                    self.sweep_orphans()
                except Exception as e:  # noqa: BLE001
                    log.warning("MQTT sweep: %s", e)
            if state.MQTT_EVENTS and self.client and self.connected:
                events, state.MQTT_EVENTS[:] = list(state.MQTT_EVENTS), []
                for e in events:
                    try:
                        self.client.publish(f"{self.base}/kuerzung", json.dumps(e), qos=1, retain=False)
                    except Exception as ex:  # noqa: BLE001
                        log.warning("MQTT Ereignis: %s", ex)
            if state.MQTT_DIRTY or time.time() - last >= self.interval:
                state.MQTT_DIRTY.clear()
                try:
                    self.publish_state()
                except Exception as e:  # noqa: BLE001
                    log.warning("MQTT publish: %s", e)
                last = time.time()

    def stop(self):
        if self.client:
            try:
                self.client.publish(f"{self.base}/status", "offline", qos=1, retain=True).wait_for_publish(2)
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
