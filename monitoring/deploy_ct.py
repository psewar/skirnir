#!/usr/bin/env python3
"""Prometheus + Grafana neben dem Router ausrollen (LXC-Container des Routers, Docker Compose). Nutzt die Helfer aus
deploy/deploy.py (Ops-Ordner, SSH, Container-Push).

    python monitoring/deploy_ct.py [--no-up] [--status]

Was passiert:
  1. Dateien nach /opt/skirnir-monitoring (compose.yaml, prometheus.yml aus der Vorlage mit dem TLS-Namen des Routers,
     Grafana-Provisionierung + Dashboard, render-monitoring.sh, systemd-Einheiten).
  2. .env: GRAFANA_HOST = Router-Hostname, CERT_DIR = Ordner des Router-Zertifikats (aus control_tls.cert der Produktiv-
     konfiguration), GRAFANA_UID = Eigentuemer des Zertifikatsschluessels (damit Grafana ihn lesen darf).
  3. render-monitoring.sh legt die Secrets ab (Metrik-Konto des Routers, Grafana-Admin) - beide kommen aus dem Secret-Store,
     nie von hier. Timer (taeglich) und Zertifikats-Watch (Grafana-Neustart bei Renewal) werden aktiviert.
  4. docker compose up -d; danach Gesundheit, Scrape-Ziel und ein erster Datenpunkt werden geprueft.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "deploy"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import yaml  # noqa: E402

from deploy import OPS, connect, ct_exec, ct_push, run  # noqa: E402

REMOTE = "/opt/skirnir-monitoring"
FILES = [  # (lokal relativ zu monitoring/, Ziel relativ zu REMOTE, mode)
    ("compose.yaml", "compose.yaml", "0644"),
    ("render-monitoring.sh", "render-monitoring.sh", "0755"),
    ("grafana/provisioning/datasources/prometheus.yaml", "grafana/provisioning/datasources/prometheus.yaml", "0644"),
    ("grafana/provisioning/dashboards/skirnir.yaml", "grafana/provisioning/dashboards/skirnir.yaml", "0644"),
    ("grafana/dashboards/skirnir.json", "grafana/dashboards/skirnir.json", "0644"),
    ("skirnir-monitoring-render.service", "skirnir-monitoring-render.service", "0644"),
    ("skirnir-monitoring-render.timer", "skirnir-monitoring-render.timer", "0644"),
    ("skirnir-monitoring-cert.service", "skirnir-monitoring-cert.service", "0644"),
]


def status(c):
    rc, out, err = run(c, ct_exec(
        f"cd {REMOTE} && docker compose ps --format '{{{{.Name}}}} {{{{.Status}}}}'; "
        "echo '--- targets'; curl -s -m 5 http://127.0.0.1:9090/api/v1/targets | python3 -c \"import json,sys; d=json.load(sys.stdin); "
        "[print(t['labels'].get('job'), t['health'], t.get('lastError') or '') for t in d['data']['activeTargets']]\"; "
        "echo '--- sample'; curl -s -m 5 'http://127.0.0.1:9090/api/v1/query?query=skirnir_node_up' | python3 -c \"import json,sys; d=json.load(sys.stdin); "
        "[print(r['metric'].get('node'), r['value'][1]) for r in d['data']['result']]\"; "
        "echo '--- grafana'; curl -s -k -m 5 https://127.0.0.1:3000/api/health; echo; systemctl is-active skirnir-monitoring-render.timer skirnir-monitoring-cert.path"))
    print(out.strip())
    return out


def main():
    no_up = "--no-up" in sys.argv
    c = connect()
    try:
        if "--status" in sys.argv:
            status(c)
            return
        host = OPS["ROUTER_HOST"]
        prod = yaml.safe_load(open(OPS["CONFIG_YAML"], encoding="utf-8"))
        cert = ((prod.get("router") or {}).get("control_tls") or {}).get("cert")
        if not cert:
            raise SystemExit("Produktivkonfiguration ohne router.control_tls.cert - Grafana braucht das Zertifikat des Routers")
        cert_dir = os.path.dirname(cert)
        rc, out, err = run(c, ct_exec(f"stat -c %u {cert_dir}/server.key 2>/dev/null || stat -c %u {cert}"))
        uid = out.strip() or "472"
        prom = open(os.path.join(HERE, "prometheus.yml.tmpl"), encoding="utf-8").read().replace("__ROUTER_HOST__", host)
        path_unit = open(os.path.join(HERE, "skirnir-monitoring-cert.path"), encoding="utf-8").read().replace("__CERT_DIR__", cert_dir)
        env = f"GRAFANA_HOST={host}\nCERT_DIR={cert_dir}\nGRAFANA_UID={uid}\n"
        run(c, "mkdir -p /tmp/skirnir-monitoring/grafana/provisioning/datasources /tmp/skirnir-monitoring/grafana/provisioning/dashboards /tmp/skirnir-monitoring/grafana/dashboards")
        sftp = c.open_sftp()
        for local, remote, _ in FILES:
            sftp.put(os.path.join(HERE, local), f"/tmp/skirnir-monitoring/{remote}")
        with sftp.open("/tmp/skirnir-monitoring/prometheus.yml", "w") as f:
            f.write(prom)
        with sftp.open("/tmp/skirnir-monitoring/skirnir-monitoring-cert.path", "w") as f:
            f.write(path_unit)
        with sftp.open("/tmp/skirnir-monitoring/.env", "w") as f:
            f.write(env)
        sftp.close()
        run(c, ct_exec(f"mkdir -p {REMOTE}/grafana/provisioning/datasources {REMOTE}/grafana/provisioning/dashboards {REMOTE}/grafana/dashboards {REMOTE}/secrets"))
        pushes = [(remote, f"{REMOTE}/{remote}", mode) for _, remote, mode in FILES]
        pushes += [("prometheus.yml", f"{REMOTE}/prometheus.yml", "0644"), (".env", f"{REMOTE}/.env", "0600"),
                   ("skirnir-monitoring-cert.path", f"{REMOTE}/skirnir-monitoring-cert.path", "0644")]
        for staged, remote, mode in pushes:
            rc, out, err = run(c, ct_push(f"/tmp/skirnir-monitoring/{staged}", remote, mode))
            if rc:
                raise SystemExit(f"push {staged}: {out} {err}")
        run(c, "rm -rf /tmp/skirnir-monitoring")
        print(f"Dateien in {REMOTE} (Grafana-UID {uid}, Zertifikate {cert_dir}, TLS-Name {host})")
        rc, out, err = run(c, ct_exec(
            f"cd {REMOTE} && for u in skirnir-monitoring-render.service skirnir-monitoring-render.timer skirnir-monitoring-cert.service skirnir-monitoring-cert.path; "
            f"do cp {REMOTE}/$u /etc/systemd/system/$u; done; systemctl daemon-reload && systemctl enable --now skirnir-monitoring-render.timer skirnir-monitoring-cert.path >/dev/null 2>&1; "
            f"GRAFANA_UID={uid} ./render-monitoring.sh 2>&1 | sed 's/=.*//'; ls -la secrets/ | tail -n +2"))
        print(out.strip(), err.strip()[:300])
        if no_up:
            return
        rc, out, err = run(c, ct_exec(f"cd {REMOTE} && docker compose pull -q 2>&1 | tail -3; docker compose up -d --force-recreate 2>&1 | tail -5"), timeout=900)
        print(out.strip()[-800:], err.strip()[-300:])
        for _ in range(24):
            time.sleep(5)
            rc, out, err = run(c, ct_exec("curl -s -m 5 http://127.0.0.1:9090/api/v1/targets"))
            try:
                targets = json.loads(out)["data"]["activeTargets"]
            except Exception:  # noqa: BLE001
                continue
            sk = [t for t in targets if t["labels"].get("job") == "skirnir"]
            if sk and sk[0]["health"] == "up":
                break
        status(c)
    finally:
        c.close()


if __name__ == "__main__":
    main()
