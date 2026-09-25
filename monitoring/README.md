# Monitoring: Prometheus + Grafana next to the router

The router exposes `/metrics` in the Prometheus text format (Basic Auth like `/admin`). Its counters survive restarts since
router 0.1.9 (`metrics.json`), but a router is not a time-series database: for history over days and weeks, alerting and
dashboards this stack runs a Prometheus with 90 days of retention and a Grafana with a provisioned dashboard, as two
containers on the router host.

| File | Purpose |
|---|---|
| `compose.yaml` | Prometheus (`127.0.0.1:9090`, retention 90 d, 512 MB) and Grafana (`https://<host>:3000`, 384 MB, no sign-up, no anonymous access) |
| `prometheus.yml.tmpl` | scrape configuration; `deploy_ct.py` fills in the router's TLS name. The target is `host.docker.internal:11435` with `tls_config.server_name`, so no hairpin through the public address |
| `grafana/provisioning/` | data source (Prometheus) and dashboard provider |
| `grafana/dashboards/skirnir.json` | dashboard "Skirnir": requests/min by role, tokens/s by model, duration p50/p95, errors and queue, GPU utilisation, VRAM, power and limit, node and guard state timelines, breaker, loaded models, generation speed, cloud spend, requests today, decisions by engine, events |
| `render-monitoring.sh` | writes the two secrets from the secret store: the router account `metrics` (`OLLAMA_ROUTER_UI_METRICS_PASS`) and the Grafana admin password (`GRAFANA_ADMIN_PASSWORD`); uses the same access as the router's `render-env.sh` |
| `skirnir-monitoring-render.{service,timer}` | daily secret refresh; a changed secret restarts the affected container |
| `skirnir-monitoring-cert.{path,service}` | restarts Grafana when the router's Let's Encrypt certificate is renewed (Grafana serves the same certificate) |
| `deploy_ct.py` | rollout from the workstation via the ops folder (`deploy/ops_env.py`): files, `.env`, units, secrets, `docker compose up`, then target health and a first sample; `--status` for a check |

Grafana runs as the owner of the certificate key (`GRAFANA_UID` in `.env`, read at deploy from the key file), so it can read
the key without a copy; its data directory is chowned accordingly. The `metrics` account must exist in the router's
`control_auth.users` with the same password the secret store holds (the ops helper creates both).

Not in this repo: `.env`, `secrets/`, `prometheus.yml`, and the data directories (see `.gitignore`).
