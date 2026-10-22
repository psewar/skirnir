"""Assembly of the two aiohttp apps, main loop, and CLI."""

import asyncio
import importlib.util
import logging
import os
import secrets
import signal
import socket
import ssl
import sys

from aiohttp import ClientSession, web

from . import admin, agentupdate, anthropic_api, auth, cloud, config, ha, metrics, nodes, ollamaupdate, openai_api, perf, poll, proxy, registry, state
from . import decision, fallback_observation, memory, rerank, session_api, context_ops
from .common import log, INFER_PATHS, FORBIDDEN_PATHS, split_listen
from .outcome import failed

# TCP keepalive on the API port: first probe after 60 s idle, then every 15 s; connection considered dead after 4 unanswered probes
KEEPALIVE = (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4))


def keepalive_socket(listen):
    """Listening socket with TCP keepalive. Trigger 2026-10-01: a non-streaming request took 6.6 min, no bytes flowed during that time;
    the connection state on the path to the client (WSL mirrored) silently expired, the finished response never arrived, and the
    router counted it as ok. Probes keep the path state fresh and let dead connections surface as errors.
    Linux propagates the options to every accepted connection (selftest_keepalive.py verifies this); where TCP_KEEP* is unavailable
    (Windows test env), it falls back to SO_KEEPALIVE with system defaults."""
    h, p = split_listen(listen)
    sock = socket.create_server((h, p))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in KEEPALIVE:
        if hasattr(socket, name):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    return sock


def build_apps():
    api = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[auth.client_auth_middleware])
    session_api.register(api)
    memory.register(api)
    api.cleanup_ctx.append(context_ops.lifecycle)
    api.router.add_get("/", proxy.handle_root)
    api.router.add_get("/api/tags", proxy.handle_tags)
    api.router.add_post("/api/show", proxy.handle_show)
    api.router.add_get("/api/ps", proxy.handle_ps)
    api.router.add_get("/api/version", proxy.handle_version)
    api.router.add_get("/v1/skirnir/availability/{model}", proxy.handle_availability)   # Register before the /v1 catch-all
    api.router.add_post("/rerank", rerank.handle_rerank)   # TEI reranking (router.rerank, default off -> 404); always registered, Reload toggles it
    api.router.add_get("/info", rerank.handle_info)        # TEI ID; Hindsight TEI client fetches it before the first /rerank
    if state.CFG.anthropic_enabled:   # Anthropic Messages API (Claude Code): custom translation to /api/chat
        api.router.add_post("/v1/messages", anthropic_api.handle_messages)
        api.router.add_post("/v1/messages/count_tokens", anthropic_api.handle_count_tokens)
    for p in INFER_PATHS:
        api.router.add_post(p, proxy.handle_infer)
    for p in FORBIDDEN_PATHS:
        api.router.add_route("*", p, proxy.handle_forbidden)
    if state.CFG.openai_enabled:   # OpenAI-compatible (Node-RED-MCP etc.); Bearer token is ignored like in Ollama
        api.router.add_get("/v1/models", openai_api.handle_oa_models)
        api.router.add_get("/v1/models/{model}", openai_api.handle_oa_model)
        api.router.add_post("/v1/chat/completions", openai_api.handle_oa_chat)
        api.router.add_post("/v1/completions", openai_api.handle_oa_completions)
        api.router.add_post("/v1/embeddings", openai_api.handle_oa_embeddings)
        api.router.add_route("*", "/v1/{rest:.*}", openai_api.handle_oa_unknown)
    ctl = web.Application(middlewares=[auth.basic_auth_middleware])
    ctl[context_ops.API_KEY] = api
    ctl.router.add_get("/admin/context", context_ops.handle)
    ctl.router.add_get("/", admin.handle_ui)
    ctl.router.add_get(r"/{name:(skirnir\.png|favicon\.png|favicon\.ico)}", admin.handle_asset)
    ctl.router.add_post("/v1/heartbeat/{node}", poll.handle_heartbeat)   # Heartbeat injection with heartbeat_token (tests, diagnostics)
    ctl.router.add_get("/v1/tunnel", registry.handle_tunnel_v2)
    ctl.router.add_get("/v1/agent/binary/{name}", agentupdate.handle_binary)   # Download with one-time token from the update task
    ctl.router.add_get("/admin/agent-update", agentupdate.handle_manifest)
    ctl.router.add_get("/admin/ollama-update", ollamaupdate.handle_status)        # Latest Ollama version, window, pending nodes
    ctl.router.add_post("/admin/ollama-update/check", ollamaupdate.handle_check)  # Version check now
    ctl.router.add_get("/admin/nodes", registry.handle_nodes)
    ctl.router.add_post("/admin/nodes/{fp}/{action}", registry.handle_node_action)
    ctl.router.add_delete("/admin/nodes/{fp}", registry.handle_node_delete)
    ctl.router.add_get("/admin/state", admin.handle_state)
    ctl.router.add_route("*", "/admin/client_auth", admin.handle_client_auth)
    ctl.router.add_get("/admin/config", admin.handle_config_get)
    ctl.router.add_put("/admin/config", admin.handle_config_put)
    ctl.router.add_post("/admin/clients/{name}/token", admin.handle_client_token)   # Clients tab: new secret (plaintext shown once)
    ctl.router.add_post("/admin/try", admin.handle_try)
    ctl.router.add_post("/admin/loadtest", admin.handle_loadtest)
    ctl.router.add_post("/admin/decide", admin.handle_decide)            # Decision Engine direkt fragen (UI, Auswertung)
    ctl.router.add_get("/admin/decision", admin.handle_decision_status)   # Load test with priority probes (UI, Try it)
    ctl.router.add_get("/admin/ha", ha.handle_ha)
    ctl.router.add_get("/admin/usage", admin.handle_usage)   # Stage 4
    ctl.router.add_get("/metrics", admin.handle_metrics)     # Stage 4: Prometheus (Basic Auth like /admin)
    ctl.router.add_post("/admin/measure", perf.handle_measure)
    ctl.router.add_post("/admin/bench", perf.handle_bench)
    return api, ctl


def install_uvloop():
    """uvloop instead of the standard loop, if installed (Router-CT: python3-uvloop 0.17). Less CPU per connection/frame;
    on Windows (tests, dev env) uvloop is unavailable -> standard loop remains."""
    if importlib.util.find_spec("uvloop") is None:
        return False
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    return True


async def main(cfg_path):
    cfg = config.Config.load(cfg_path)
    if failed(cfg):
        log.error("configuration invalid, router does not start: %s", cfg.message)
        sys.exit(2)
    state.CFG = cfg
    state.INTERNAL_TOKEN = secrets.token_urlsafe(32)   # Self-call of the UI (Try it) in enforce mode, localhost only
    decision.init()   # Auto role (decision_engine), if configured
    state.NODES = {name: nodes.Node(name, spec) for name, spec in state.CFG.nodes.items()}
    state.REG = registry.NodeRegistry(os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "nodes.json")).load()
    for fp, e in list(state.REG.nodes.items()):
        if e.get("state") == "approved":
            registry.activate_node(fp)
    log.info("node register: %d entries (%d approved)", len(state.REG.nodes), sum(1 for e in state.REG.nodes.values() if e["state"] == "approved"))
    perf.perf_load()
    fallback_observation.adopt_file_before_0_9()   # anfragen.jsonl -> requests.jsonl (0.9.0)
    metrics.usage_load()
    n_met = metrics.metrics_load()
    if n_met:
        log.info("metrics: %d series taken from metrics.json (total since %s)", n_met, state.METRICS.get("since"))
    n_dec = state.decisions_load()
    if n_dec:
        log.info("decision journal: %d entries taken from events.jsonl", n_dec)
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
        log.warning("basic auth without TLS: passwords cross the network in plain text (set control_tls)")
    if state.CFG.api_tls and ctl_ssl is None:
        log.error("api_tls: true needs control_tls (certificate) - router does not start")
        sys.exit(2)
    for app, listen, sslctx, keepalive in ((api, state.CFG.listen, ctl_ssl if state.CFG.api_tls else None, True),
                                           (ctl, state.CFG.control_listen, ctl_ssl, False)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        h, p = split_listen(listen)
        if keepalive:   # API port: long requests without stream (minutes without bytes) - see keepalive_socket
            await web.SockSite(runner, keepalive_socket(listen), ssl_context=sslctx).start()
        else:           # Control port: tunnel has its own heartbeats, UI/admin requests are short
            await web.TCPSite(runner, h, p, ssl_context=sslctx).start()
        runners.append(runner)
        log.info("listening on %s://%s:%d%s", "https" if sslctx else "http", h, p, " (TCP-Keepalive)" if keepalive else "")
    tasks = [asyncio.create_task(poll.poll_loop()), asyncio.create_task(poll.tick_loop()), asyncio.create_task(agentupdate.rollout_loop()),
             asyncio.create_task(ollamaupdate.rollout_loop())]
    ha_pub = None
    if state.CFG.mqtt and state.CFG.mqtt.get("host"):
        ha_pub = ha.HAPublisher(state.CFG.mqtt)
        state.HA_PUB = ha_pub
        ha_pub.start()
        tasks.append(asyncio.create_task(ha_pub.loop()))

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def on_hup():
        problem = state.CFG.reload()   # Checks against a probe first: invalid = nothing changed
        if failed(problem):
            log.error("config reload failed, keeping old: %s", problem.message)
            return
        cloud.setup()
        decision.ensure()
        log.info("config reloaded")

    if sys.platform != "win32":   # Windows (test env only): the event loop does not support signal handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        loop.add_signal_handler(signal.SIGHUP, on_hup)
    await stop.wait()
    if ha_pub:
        ha_pub.stop()
    for t in tasks:
        t.cancel()
    # Close open tunnels, otherwise runner.cleanup() waits for WebSocket handlers (29 s in deploy 2026-09-07)
    for tun in [n.tunnel for n in state.NODES.values() if n.tunnel is not None] + list(state.PENDING.values()):
        await asyncio.wait({asyncio.ensure_future(registry.close_ws(tun.ws))}, timeout=3)
    if state.REG is not None and state.REG.dirty:
        state.REG.save()
    metrics.usage_save()     # Last state instead of up to 10 s loss
    metrics.metrics_save()   # Counters/histograms survive restart
    for r in runners:
        await r.cleanup()
    await state.SESSION.close()


def cli():
    """CLI: `router.py <config.yaml>` or `router.py --hash <password>`."""
    if len(sys.argv) > 2 and sys.argv[1] == "--hash":
        print(auth.hash_password(sys.argv[2]))      # Hilfsfunktion: python3 router.py --hash '<passwort>'
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "--check":
        # Stage 6: Validate configuration against schema and parser (deploy.py: local without, on CT with roles.yaml).
        # rc 0 = ok, 2 = invalid. --pure ignores an adjacent roles.yaml.
        cfg = config.Config.load(sys.argv[2], use_overrides="--pure" not in sys.argv)
        if failed(cfg):
            print(f"ERROR: {cfg.message}")
            sys.exit(2)
        print(f"configuration ok: {len(cfg.roles)} roles, {len(cfg.models)} models in the catalog, {len(cfg.nodes)} static nodes, "
              f"client_auth {cfg.client_auth['mode']}" + ("" if "--pure" in sys.argv or not os.path.exists(cfg.overrides_path) else " (with roles.yaml)"))
        sys.exit(0)
    logging.basicConfig(level=os.environ.get("ROUTER_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    uv = install_uvloop()
    logging.getLogger('skirnir-router').info('event loop: %s', 'uvloop' if uv else 'asyncio')
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "/etc/skirnir-router/config.yaml"))
