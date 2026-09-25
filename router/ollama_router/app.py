"""Zusammenbau der beiden aiohttp-Apps, Hauptschleife, Kommandozeile."""

import asyncio
import logging
import os
import secrets
import signal
import ssl
import sys

from aiohttp import ClientSession, web

from . import admin, agentupdate, auth, cloud, config, ha, metrics, nodes, openai_api, perf, poll, proxy, registry, state
from . import decision
from .common import log, INFER_PATHS, FORBIDDEN_PATHS, split_listen


def build_apps():
    api = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[auth.client_auth_middleware])
    api.router.add_get("/", proxy.handle_root)
    api.router.add_get("/api/tags", proxy.handle_tags)
    api.router.add_post("/api/show", proxy.handle_show)
    api.router.add_get("/api/ps", proxy.handle_ps)
    api.router.add_get("/api/version", proxy.handle_version)
    for p in INFER_PATHS:
        api.router.add_post(p, proxy.handle_infer)
    for p in FORBIDDEN_PATHS:
        api.router.add_route("*", p, proxy.handle_forbidden)
    if state.CFG.openai_enabled:   # OpenAI-kompatibel (Node-RED-MCP u. a.); Bearer-Token wird wie bei Ollama ignoriert
        api.router.add_get("/v1/models", openai_api.handle_oa_models)
        api.router.add_get("/v1/models/{model}", openai_api.handle_oa_model)
        api.router.add_post("/v1/chat/completions", openai_api.handle_oa_chat)
        api.router.add_post("/v1/completions", openai_api.handle_oa_completions)
        api.router.add_post("/v1/embeddings", openai_api.handle_oa_embeddings)
        api.router.add_route("*", "/v1/{rest:.*}", openai_api.handle_oa_unknown)
    ctl = web.Application(middlewares=[auth.basic_auth_middleware])
    ctl.router.add_get("/", admin.handle_ui)
    ctl.router.add_get(r"/{name:(skirnir\.png|favicon\.png|favicon\.ico)}", admin.handle_asset)
    ctl.router.add_post("/v1/heartbeat/{node}", poll.handle_heartbeat)
    ctl.router.add_get("/v1/tunnel/{node}", registry.handle_tunnel)
    ctl.router.add_get("/v1/tunnel", registry.handle_tunnel_v2)
    ctl.router.add_get("/v1/agent/binary/{name}", agentupdate.handle_binary)   # Download mit Einmal-Token aus dem Update-Auftrag
    ctl.router.add_get("/admin/agent-update", agentupdate.handle_manifest)
    ctl.router.add_get("/admin/nodes", registry.handle_nodes)
    ctl.router.add_post("/admin/nodes/{fp}/{action}", registry.handle_node_action)
    ctl.router.add_delete("/admin/nodes/{fp}", registry.handle_node_delete)
    ctl.router.add_get("/admin/state", admin.handle_state)
    ctl.router.add_route("*", "/admin/client_auth", admin.handle_client_auth)
    ctl.router.add_get("/admin/config", admin.handle_config_get)
    ctl.router.add_put("/admin/config", admin.handle_config_put)
    ctl.router.add_post("/admin/clients/{name}/token", admin.handle_client_token)   # Clients-Tab: neues Secret (Klartext einmalig)
    ctl.router.add_post("/admin/try", admin.handle_try)
    ctl.router.add_post("/admin/loadtest", admin.handle_loadtest)
    ctl.router.add_post("/admin/decide", admin.handle_decide)            # Decision Engine direkt fragen (UI, Auswertung)
    ctl.router.add_get("/admin/decision", admin.handle_decision_status)   # Lasttest mit Prioritaets-Sonden (UI, Ausprobieren)
    ctl.router.add_get("/admin/ha", ha.handle_ha)
    ctl.router.add_get("/admin/usage", admin.handle_usage)   # Stufe 4
    ctl.router.add_get("/metrics", admin.handle_metrics)     # Stufe 4: Prometheus (Basic Auth wie /admin)
    ctl.router.add_post("/admin/measure", perf.handle_measure)
    ctl.router.add_post("/admin/bench", perf.handle_bench)
    return api, ctl


def install_uvloop():
    """uvloop statt der Standard-Schleife, wenn installiert (Router-CT: python3-uvloop 0.17). Weniger CPU je Verbindung/Frame;
    unter Windows (Tests, Dev-Env) gibt es kein uvloop -> Standard bleibt."""
    try:
        import uvloop
    except ImportError:
        return False
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    return True


async def main(cfg_path):
    state.CFG = config.Config(cfg_path)
    state.INTERNAL_TOKEN = secrets.token_urlsafe(32)   # Selbstaufruf der UI (Ausprobieren) im enforce-Modus, nur localhost
    decision.init()   # Auto-Rolle (decision_engine), wenn konfiguriert
    state.NODES = {name: nodes.Node(name, spec) for name, spec in state.CFG.nodes.items()}
    state.REG = registry.NodeRegistry(os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "nodes.json")).load()
    for fp, e in list(state.REG.nodes.items()):
        if e.get("state") == "approved":
            registry.activate_node(fp)
    log.info("Knotenregister: %d Eintraege (%d freigegeben)", len(state.REG.nodes), sum(1 for e in state.REG.nodes.values() if e["state"] == "approved"))
    perf.perf_load()
    metrics.usage_load()
    n_dec = state.decisions_load()
    if n_dec:
        log.info("Entscheidungsprotokoll: %d Eintraege aus events.jsonl uebernommen", n_dec)
    cloud.setup()
    state.SESSION = ClientSession()
    api, ctl = build_apps()
    runners = []
    ctl_ssl = None
    if state.CFG.control_tls:
        ctl_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctl_ssl.minimum_version = ssl.TLSVersion.TLSv1_2
        ctl_ssl.load_cert_chain(state.CFG.control_tls[0], state.CFG.control_tls[1])
    elif state.CFG.control_users:
        log.warning("Basic Auth ohne TLS: Passwoerter gehen im Klartext ueber das Netz (control_tls setzen)")
    if state.CFG.api_tls and ctl_ssl is None:
        raise SystemExit("api_tls: true braucht control_tls (Zertifikat)")
    for app, listen, sslctx in ((api, state.CFG.listen, ctl_ssl if state.CFG.api_tls else None), (ctl, state.CFG.control_listen, ctl_ssl)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        h, p = split_listen(listen)
        await web.TCPSite(runner, h, p, ssl_context=sslctx).start()
        runners.append(runner)
        log.info("listening on %s://%s:%d", "https" if sslctx else "http", h, p)
    tasks = [asyncio.create_task(poll.poll_loop()), asyncio.create_task(poll.tick_loop()), asyncio.create_task(agentupdate.rollout_loop())]
    ha_pub = None
    if state.CFG.mqtt and state.CFG.mqtt.get("host"):
        ha_pub = ha.HAPublisher(state.CFG.mqtt)
        state.HA_PUB = ha_pub
        ha_pub.start()
        tasks.append(asyncio.create_task(ha_pub.loop()))

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def on_hup():
        try:
            state.CFG.reload()
            cloud.setup()
            decision.ensure()
            log.info("config reloaded")
        except Exception as e:  # noqa: BLE001
            log.error("config reload failed, keeping old: %s", e)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        if hasattr(signal, "SIGHUP"):
            loop.add_signal_handler(signal.SIGHUP, on_hup)
    except NotImplementedError:  # Windows (nur Testbetrieb)
        pass
    await stop.wait()
    if ha_pub:
        ha_pub.stop()
    for t in tasks:
        t.cancel()
    # offene Tunnel schliessen, sonst wartet runner.cleanup() auf die WebSocket-Handler (29 s beim Deploy 2026-09-07)
    for tun in [n.tunnel for n in state.NODES.values() if n.tunnel is not None] + list(state.PENDING.values()):
        try:
            await asyncio.wait_for(tun.ws.close(), 3)
        except Exception:  # noqa: BLE001
            pass
    if state.REG is not None and state.REG.dirty:
        state.REG.save()
    for r in runners:
        await r.cleanup()
    await state.SESSION.close()


def cli():
    """Kommandozeile: `router.py <config.yaml>` oder `router.py --hash <passwort>`."""
    if len(sys.argv) > 2 and sys.argv[1] == "--hash":
        print(auth.hash_password(sys.argv[2]))      # Hilfsfunktion: python3 router.py --hash '<passwort>'
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "--check":
        # Stufe 6: Konfiguration gegen Schema und Parser pruefen (deploy.py: lokal ohne, auf dem CT mit roles.yaml).
        # rc 0 = ok, 2 = ungueltig. --pure ignoriert eine daneben liegende roles.yaml.
        try:
            cfg = config.Config(sys.argv[2], use_overrides="--pure" not in sys.argv)
            print(f"Konfiguration ok: {len(cfg.roles)} Rollen, {len(cfg.models)} Modelle im Katalog, {len(cfg.nodes)} statische Knoten, "
                  f"client_auth {cfg.client_auth['mode']}" + ("" if "--pure" in sys.argv or not os.path.exists(cfg.overrides_path) else " (mit roles.yaml)"))
            sys.exit(0)
        except Exception as e:  # noqa: BLE001
            print(f"FEHLER: {e}")
            sys.exit(2)
    logging.basicConfig(level=os.environ.get("ROUTER_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    uv = install_uvloop()
    logging.getLogger('ollama-router').info('Event-Loop: %s', 'uvloop' if uv else 'asyncio')
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "/etc/ollama-router/config.yaml"))
