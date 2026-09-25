#!/bin/bash
# skirnir-router: Laufzeit-Secrets aus dem Secret-Store rendern (Universal-Auth-API).
# Zugang steht NICHT hier, sondern in /etc/skirnir-router/render-env.conf (deploy.py legt sie aus dem Ops-Ordner ab, 0600):
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
CONF=/etc/skirnir-router/render-env.conf
[ -r "$CONF" ] || { echo "$CONF fehlt - nichts gerendert"; exit 0; }
. "$CONF"
[ -n "$SECRET_STORE_AUTH_FILE" ] && . "$SECRET_STORE_AUTH_FILE"
[ -n "$SECRET_STORE_DOMAIN" -a -n "$SECRET_STORE_CLIENT_ID" -a -n "$SECRET_STORE_CLIENT_SECRET" -a -n "$PROJECT_ID" ] || { echo "render-env.conf unvollstaendig - nichts gerendert"; exit 0; }
ENVIRONMENT=${ENVIRONMENT:-prod}
OUT=/etc/skirnir-router/secrets.env
# Login-Body per stdin (-d @-), damit das Client-Secret nicht in der Prozessliste steht
T=$(printf '{"clientId":"%s","clientSecret":"%s"}' "$SECRET_STORE_CLIENT_ID" "$SECRET_STORE_CLIENT_SECRET" \
     | curl -s -m 15 "$SECRET_STORE_DOMAIN/api/v1/auth/universal-auth/login" -H 'Content-Type: application/json' -d @- | jq -r .accessToken)
[ -z "$T" -o "$T" = "null" ] && { echo "Secret-Store-Login fehlgeschlagen - behalte bestehende $OUT"; exit 0; }
g(){ curl -s -m 15 "$SECRET_STORE_DOMAIN/api/v3/secrets/raw/$1?workspaceId=$PROJECT_ID&environment=$ENVIRONMENT&secretPath=%2F" -H "Authorization: Bearer $T" | jq -r .secret.secretValue; }
M=$(g MQTT_PASSWORD)
[ -z "$M" -o "$M" = "null" ] && { echo "Secret-Fetch fehlgeschlagen (Secret-Store nicht erreichbar?) - behalte bestehende $OUT"; exit 0; }
TMP=$(mktemp); printf 'MQTT_PASSWORD=%s\n' "$M" > "$TMP"; chmod 600 "$TMP"
# Stufe 5 Cloud: Anbieter-Schluessel sind optional (fehlt einer im Secret-Store, bleibt der Anbieter im Router ohne Schluessel = keine Stufe)
for K in OPENAI_API_KEY ANTHROPIC_API_KEY; do
  V=$(g "$K"); [ -n "$V" -a "$V" != "null" ] && printf '%s=%s\n' "$K" "$V" >> "$TMP"
done
if [ ! -f "$OUT" ] || ! cmp -s "$TMP" "$OUT"; then
  mv "$TMP" "$OUT"; echo "$(date) $OUT aus dem Secret-Store aktualisiert"
  if [ "$RESTART" = 1 ] && systemctl is-active --quiet skirnir-router; then
    systemctl restart --no-block skirnir-router && echo "skirnir-router Neustart angestossen (Secret geaendert)"
  fi
else
  rm -f "$TMP"; echo "$(date) $OUT unveraendert"
fi
