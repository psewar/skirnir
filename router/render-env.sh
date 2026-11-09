#!/bin/bash
# skirnir-router: Laufzeit-Secrets aus dem Secret-Store rendern (Universal-Auth-API).
# Zugang steht NICHT hier, sondern in /etc/skirnir-router-secrets/render-env.conf (Ordner 0700 root, ADR-0073) (deploy.py legt sie aus dem Ops-Ordner ab, 0600):
#   SECRET_STORE_DOMAIN, SECRET_STORE_CLIENT_ID, SECRET_STORE_CLIENT_SECRET  (Universal Auth, Rolle viewer) - direkt oder
#   ueber SECRET_STORE_AUTH_FILE (Datei, die diese drei Variablen setzt)
#   PROJECT_ID           Secret-Store-Projekt des Routers      ENVIRONMENT  z. B. prod
# Schreibt /etc/skirnir-router/secrets.env (600), das skirnir-router.service als EnvironmentFile liest.
# Bei Ausfall des Secret-Stores bleibt die letzte Datei stehen.
# Aufruf ohne Argument (als Abhaengigkeit von skirnir-router.service): NUR rendern, NIE den Router anfassen -
# sonst Job-Verklemmung in systemd (Start wartet auf uns, wir warten auf seinen Restart).
# Aufruf mit --restart (taeglicher Timer): bei geaendertem Secret den Router neu starten.
set -e
RESTART=0; [ "$1" = "--restart" ] && RESTART=1
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
[ -n "$SECRET_STORE_DOMAIN" -a -n "$SECRET_STORE_CLIENT_ID" -a -n "$SECRET_STORE_CLIENT_SECRET" -a -n "$PROJECT_ID" ] || { echo "render-env.conf unvollstaendig - nichts gerendert"; exit 0; }
ENVIRONMENT=${ENVIRONMENT:-prod}
OUT=${RENDER_ENV_OUT:-/etc/skirnir-router/secrets.env}
# Login-Body per stdin (-d @-), damit das Client-Secret nicht in der Prozessliste steht
T=$(printf '{"clientId":"%s","clientSecret":"%s"}' "$SECRET_STORE_CLIENT_ID" "$SECRET_STORE_CLIENT_SECRET" \
     | curl -s -m 15 "$SECRET_STORE_DOMAIN/api/v1/auth/universal-auth/login" -H 'Content-Type: application/json' -d @- | jq -r .accessToken)
[ -z "$T" -o "$T" = "null" ] && { echo "Secret-Store-Login fehlgeschlagen - behalte bestehende $OUT"; exit 0; }
g(){ curl -s -m 15 "$SECRET_STORE_DOMAIN/api/v3/secrets/raw/$1?workspaceId=$PROJECT_ID&environment=$ENVIRONMENT&secretPath=%2F" -H "Authorization: Bearer $T" | jq -r .secret.secretValue; }
M=$(g MQTT_PASSWORD)
[ -z "$M" -o "$M" = "null" ] && { echo "Secret-Fetch fehlgeschlagen (Secret-Store nicht erreichbar?) - behalte bestehende $OUT"; exit 0; }
TMP=$(mktemp); printf 'MQTT_PASSWORD=%s\n' "$M" > "$TMP"; chmod 600 "$TMP"
# Stufe 5 Cloud: Anbieter-Schluessel sind optional (fehlt einer im Secret-Store, bleibt der Anbieter im Router ohne Schluessel = keine Stufe)
# Optionale Gedaechtnisschicht (design/session-store.md, design/hindsight-memory.md): nur vorhandene Secrets landen in der
# Datei; ohne sie bleibt die Schicht aus bzw. startet nicht (der Router meldet beim Start, welche Variable fehlt).
for K in OPENAI_API_KEY ANTHROPIC_API_KEY SKIRNIR_SESSION_DSN SKIRNIR_MEMORY_NAMESPACE_KEY SKIRNIR_HINDSIGHT_URL SKIRNIR_HINDSIGHT_TOKEN SKIRNIR_HANDOFF_KEY; do
  V=$(g "$K"); [ -n "$V" -a "$V" != "null" ] && printf '%s=%s\n' "$K" "$V" >> "$TMP"
done
if [ ! -f "$OUT" ] || ! cmp -s "$TMP" "$OUT"; then
  mv "$TMP" "$OUT"; echo "$(date) $OUT aus dem Secret-Store aktualisiert"
  if [ "$RESTART" = 1 ] && systemctl is-active --quiet skirnir-router; then
    systemctl restart --no-block skirnir-router && echo "skirnir-router Neustart angestoßen (Secret geaendert)"
  fi
else
  rm -f "$TMP"; echo "$(date) $OUT unveraendert"
fi
