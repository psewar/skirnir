#!/bin/bash
# skirnir-monitoring: Secrets aus dem Secret-Store rendern (Universal-Auth-API, gleicher Zugang wie render-env.sh des Routers):
#   OLLAMA_ROUTER_UI_METRICS_PASS -> secrets/metrics.pass   (Prometheus: Basic Auth am Router-/metrics)
#   GRAFANA_ADMIN_PASSWORD        -> secrets/grafana_admin  (Grafana: GF_SECURITY_ADMIN_PASSWORD__FILE)
# Zugang: /etc/ollama-router/render-env.conf (SECRET_STORE_DOMAIN/CLIENT_ID/CLIENT_SECRET, PROJECT_ID, ENVIRONMENT).
# Bei Ausfall des Secret-Stores bleiben die letzten Dateien stehen. Aendert sich ein Secret, wird der betroffene Container
# neu gestartet (nur mit --restart, sonst reines Rendern - fuer den ersten Lauf vor `docker compose up`).
set -e
RESTART=0; [ "$1" = "--restart" ] && RESTART=1
DIR=$(cd "$(dirname "$0")" && pwd)
CONF=/etc/ollama-router/render-env.conf
[ -r "$CONF" ] || { echo "$CONF fehlt - nichts gerendert"; exit 0; }
. "$CONF"
[ -n "$SECRET_STORE_AUTH_FILE" ] && . "$SECRET_STORE_AUTH_FILE"
[ -n "$SECRET_STORE_DOMAIN" -a -n "$SECRET_STORE_CLIENT_ID" -a -n "$SECRET_STORE_CLIENT_SECRET" -a -n "$PROJECT_ID" ] || { echo "render-env.conf unvollstaendig"; exit 0; }
ENVIRONMENT=${ENVIRONMENT:-prod}
T=$(printf '{"clientId":"%s","clientSecret":"%s"}' "$SECRET_STORE_CLIENT_ID" "$SECRET_STORE_CLIENT_SECRET" \
     | curl -s -m 15 "$SECRET_STORE_DOMAIN/api/v1/auth/universal-auth/login" -H 'Content-Type: application/json' -d @- | jq -r .accessToken)
[ -z "$T" -o "$T" = "null" ] && { echo "Secret-Store-Login fehlgeschlagen - behalte bestehende Dateien"; exit 0; }
g(){ curl -s -m 15 "$SECRET_STORE_DOMAIN/api/v3/secrets/raw/$1?workspaceId=$PROJECT_ID&environment=$ENVIRONMENT&secretPath=%2F" -H "Authorization: Bearer $T" | jq -r .secret.secretValue; }
mkdir -p "$DIR/secrets"; chmod 750 "$DIR/secrets"
render(){ # $1 Secret-Key, $2 Zieldatei, $3 Eigentuemer-UID, $4 Container
  V=$(g "$1")
  if [ -z "$V" -o "$V" = "null" ]; then echo "$1: nicht im Secret-Store - behalte bestehende $2"; return 0; fi
  [ -d "$2" ] && rm -rf "$2"   # Docker legt fuer einen fehlenden Bind-Mount ein Verzeichnis an - weg damit
  TMP=$(mktemp); printf '%s' "$V" > "$TMP"; chmod 400 "$TMP"; chown "$3" "$TMP"
  if [ ! -f "$2" ] || ! cmp -s "$TMP" "$2"; then
    mv "$TMP" "$2"; echo "$(date) $2 aktualisiert"
    if [ "$RESTART" = 1 ]; then docker restart "$4" >/dev/null 2>&1 && echo "$4 neu gestartet (Secret geaendert)"; fi
  else
    rm -f "$TMP"
  fi
  return 0   # set -e: ein &&-Rest mit Status 1 als letztes Kommando liess den ersten Lauf hier abbrechen (2026-09-25)
}
render OLLAMA_ROUTER_UI_METRICS_PASS "$DIR/secrets/metrics.pass" 65534 skirnir-prometheus
render GRAFANA_ADMIN_PASSWORD "$DIR/secrets/grafana_admin" "${GRAFANA_UID:-472}" skirnir-grafana
# Grafana-Datenverzeichnis gehoert dem Grafana-Benutzer (UID aus .env, damit der TLS-Schluessel des Hosts lesbar ist)
[ -f "$DIR/.env" ] && . "$DIR/.env"
mkdir -p "$DIR/grafana-data" "$DIR/prometheus-data"
chown -R "${GRAFANA_UID:-472}" "$DIR/grafana-data"; chown -R 65534:65534 "$DIR/prometheus-data"
chown "${GRAFANA_UID:-472}" "$DIR/secrets/grafana_admin" 2>/dev/null || true
