#!/bin/bash
# skirnir-monitoring: Secrets aus dem Secret-Store rendern (Universal-Auth-API, gleicher Zugang wie render-env.sh des Routers):
#   OLLAMA_ROUTER_UI_METRICS_PASS -> secrets/metrics.pass   (Prometheus: Basic Auth am Router-/metrics)
#   GRAFANA_ADMIN_PASSWORD        -> secrets/grafana_admin  (Grafana: GF_SECURITY_ADMIN_PASSWORD__FILE)
# Zugang: /etc/skirnir-router-secrets/render-env.conf (Ordner 0700 root, ADR-0073) (SECRET_STORE_DOMAIN/CLIENT_ID/CLIENT_SECRET, PROJECT_ID, ENVIRONMENT).
# Bei Ausfall des Secret-Stores bleiben die letzten Dateien stehen. Aendert sich ein Secret, wird der betroffene Container
# neu gestartet (nur mit --restart, sonst reines Rendern - fuer den ersten Lauf vor `docker compose up`).
set -e
RESTART=0; [ "$1" = "--restart" ] && RESTART=1
DIR=$(cd "$(dirname "$0")" && pwd)
# The access file is SOURCED AS ROOT. It lives outside every path the router service may write (ADR-0073): the router runs
# as root without capabilities, so it can still rewrite root-owned files in /etc/skirnir-router; a timer that sourced a file
# from there would run the router's code with full root. The checks below refuse instead of sourcing.
CONF=${RENDER_ENV_CONF:-/etc/skirnir-router-secrets/render-env.conf}
OLD_CONF=/etc/skirnir-router/render-env.conf
ROUTER_WRITABLE=${RENDER_ENV_ROUTER_WRITABLE:-/etc/skirnir-router /var/lib/skirnir-router /var/log/skirnir-router}
OWNER_UID=${RENDER_ENV_OWNER_UID:-0}
not_router_writable() {   # $1 = file to source; prints the reason and fails if the router could have written it
  local f d w
  f=$(readlink -f -- "$1") || { echo "$1: cannot resolve - not read"; return 1; }
  d=$(dirname -- "$f")
  for w in $ROUTER_WRITABLE; do
    case "$f/" in "$w"/*) echo "$1 lies in $w, which the router may write - not read (move it, ADR-0073)"; return 1 ;; esac
  done
  for p in "$f" "$d"; do
    [ "$(stat -c %u -- "$p")" = "$OWNER_UID" ] || { echo "$p is not owned by root - not read"; return 1; }
    [ $(( 0$(stat -c %a -- "$p") & 022 )) -eq 0 ] || { echo "$p is writable by group or others - not read"; return 1; }
  done
}
if [ ! -e "$CONF" ]; then
  [ -e "$OLD_CONF" ] && { echo "$OLD_CONF is no longer read (the router may write that folder); move it to $CONF - deploy.py does it. Nothing rendered."; exit 0; }
  echo "$CONF missing - nothing rendered"; exit 0
fi
not_router_writable "$CONF" || exit 0
. "$CONF"
if [ -n "$SECRET_STORE_AUTH_FILE" ]; then
  not_router_writable "$SECRET_STORE_AUTH_FILE" || exit 0
  . "$SECRET_STORE_AUTH_FILE"
fi
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
  return 0   # set -e: ein &&-Rest mit Status 1 als letztes Kommando ließ den ersten Lauf hier abbrechen (2026-09-25)
}
render OLLAMA_ROUTER_UI_METRICS_PASS "$DIR/secrets/metrics.pass" 65534 skirnir-prometheus
render GRAFANA_ADMIN_PASSWORD "$DIR/secrets/grafana_admin" "${GRAFANA_UID:-472}" skirnir-grafana
# Grafana-Datenverzeichnis gehoert dem Grafana-Benutzer (UID aus .env, damit der TLS-Schluessel des Hosts lesbar ist)
[ -f "$DIR/.env" ] && . "$DIR/.env"
mkdir -p "$DIR/grafana-data" "$DIR/prometheus-data"
chown -R "${GRAFANA_UID:-472}" "$DIR/grafana-data"; chown -R 65534:65534 "$DIR/prometheus-data"
chown "${GRAFANA_UID:-472}" "$DIR/secrets/grafana_admin" 2>/dev/null || true
