#!/bin/bash
# skirnir-router: render the runtime secrets into /etc/skirnir-router/secrets.env (ADR-0104).
#
# Configuration: /etc/skirnir-router-secrets/render-env.conf (folder 0700 root, outside everything the router may write,
# ADR-0073; template render-env.conf.example; deploy.py places it). SECRET_SOURCE picks where the values come from:
#   store (default)  a secret store with a universal-auth API: SECRET_STORE_DOMAIN, SECRET_STORE_CLIENT_ID and
#                    SECRET_STORE_CLIENT_SECRET (role viewer) directly or from SECRET_STORE_AUTH_FILE; PROJECT_ID, ENVIRONMENT
#   file             SECRET_FILE: a root-owned KEY=value file (0600 or stricter) outside the router's paths, read line by
#                    line and never sourced
# Without render-env.conf nothing is rendered; secrets.env can then be maintained by hand (docs/install-router.md#secrets).
#
# Steps: (1) the source is ready - checked on its own and reported as a fact (store: the answer to the login; file: the
# file checks); (2) every name is fetched on its own: present, missing or failed; (3) every present name is rendered.
# MQTT_PASSWORD is one optional name like the others. A failed fetch, or a source that holds none of the names, keeps the
# existing file, so a transient fault never drops a secret.
# secrets.env (root:skirnir-router 0640, ADR-0086) is the EnvironmentFile of skirnir-router.service; the router also reads
# it raw (mqtt.password_file, a provider's secrets_file). Its folder belongs to root, so this root script follows no link
# the router could have placed.
# The last line is always STATE: written | unchanged | kept | not-configured | refused.
#
# Without an argument (dependency of skirnir-router.service): only render, never touch the router - otherwise systemd
# deadlocks (the start waits for us, we would wait for its restart).
# With --restart (daily timer): restart the router when secrets.env changed.
set -e
RESTART=0; [ "$1" = "--restart" ] && RESTART=1
finish() { echo "STATE: $1"; exit 0; }
# The access file is SOURCED AS ROOT. It lives outside every path the router service may write (ADR-0073): the router runs
# as root without capabilities, so it can still rewrite root-owned files in /etc/skirnir-router; a timer that sourced a file
# from there would run the router's code with full root. The checks below refuse instead of sourcing.
CONF=${RENDER_ENV_CONF:-/etc/skirnir-router-secrets/render-env.conf}
OLD_CONF=/etc/skirnir-router/render-env.conf
ROUTER_WRITABLE=${RENDER_ENV_ROUTER_WRITABLE:-/etc/skirnir-router /var/lib/skirnir-router /var/log/skirnir-router}
OWNER_UID=${RENDER_ENV_OWNER_UID:-0}
not_router_writable() {   # $1 = file to read as root; prints the reason and fails if the router could have written it
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
  [ -e "$OLD_CONF" ] && { echo "$OLD_CONF is no longer read (the router may write that folder); move it to $CONF - deploy.py does it. Nothing rendered."; finish not-configured; }
  echo "$CONF missing - nothing rendered"; finish not-configured
fi
not_router_writable "$CONF" || finish refused
. "$CONF"
SECRET_SOURCE=${SECRET_SOURCE:-store}
OUT=${RENDER_ENV_OUT:-/etc/skirnir-router/secrets.env}

# --- the names --------------------------------------------------------------------------------------------------------
# secrets.env has one KEY=value per line and is the router unit's EnvironmentFile: a value with a line break would start a
# further variable of the source writer's choosing (LD_PRELOAD=..., PATH=...). Such values are refused.
one_line(){ case "$1" in *$'\n'*|*$'\r'*) return 1 ;; esac; }
# Cloud tier: provider keys are optional (a missing one leaves the provider without a key = no tier). Optional memory layers
# (design/session-store.md, design/hindsight-memory.md): only present secrets reach the file; without them the layer stays
# off or does not start (the router names the missing variable at start). MQTT_PASSWORD is needed only with an mqtt: section.
BUILTIN_KEYS="MQTT_PASSWORD OPENAI_API_KEY ANTHROPIC_API_KEY SKIRNIR_SESSION_DSN SKIRNIR_MEMORY_NAMESPACE_KEY SKIRNIR_HINDSIGHT_URL SKIRNIR_HINDSIGHT_TOKEN SKIRNIR_HANDOFF_KEY"
# Further optional secrets, e.g. the keys of additional OpenAI-compatible cloud providers (ADR-0080): their names go into
# render-env.conf as EXTRA_SECRET_KEYS="EXAMPLE_API_KEY OTHER_API_KEY". A name must start with a capital letter, contain only
# capital letters, digits and "_" (it ends up in the query URL and in secrets.env) and end in _KEY or _TOKEN: secrets.env is
# the router unit's EnvironmentFile, so a name like LD_PRELOAD, PATH or PYTHONPATH would let a value from the source steer
# the router process. Anything else is reported and skipped. Names already built in are not repeated.
KEYS="$BUILTIN_KEYS"
set -f   # no globbing while splitting the list ("*" must stay a word, not a file list)
for K in ${EXTRA_SECRET_KEYS:-}; do
  case "$K" in
    [A-Z]*) ;;
    *) echo "EXTRA_SECRET_KEYS: '$K' is not a valid name - skipped"; continue ;;
  esac
  case "$K" in
    *[!A-Z0-9_]*) echo "EXTRA_SECRET_KEYS: '$K' is not a valid name - skipped"; continue ;;
  esac
  case "$K" in
    ?*_KEY|?*_TOKEN) ;;
    *) echo "EXTRA_SECRET_KEYS: '$K' does not end in _KEY or _TOKEN - skipped"; continue ;;
  esac
  case " $KEYS " in
    *" $K "*) continue ;;
  esac
  KEYS="$KEYS $K"
done
set +f
is_key() { case " $KEYS " in *" $1 "*) return 0 ;; esac; return 1; }

# --- step 1: the source is ready (a fact of its own, never inferred from a missing value) -------------------------------
# Each source defines fetch NAME, which sets FOUND (present | missing | failed), VALUE and DETAIL.
case "$SECRET_SOURCE" in
  store)
    if [ -n "$SECRET_STORE_AUTH_FILE" ]; then
      not_router_writable "$SECRET_STORE_AUTH_FILE" || finish refused
      . "$SECRET_STORE_AUTH_FILE"
    fi
    [ -n "$SECRET_STORE_DOMAIN" -a -n "$SECRET_STORE_CLIENT_ID" -a -n "$SECRET_STORE_CLIENT_SECRET" -a -n "$PROJECT_ID" ] \
      || { echo "render-env.conf incomplete for SECRET_SOURCE=store (SECRET_STORE_DOMAIN, SECRET_STORE_CLIENT_ID, SECRET_STORE_CLIENT_SECRET, PROJECT_ID) - nothing rendered"; finish refused; }
    ENVIRONMENT=${ENVIRONMENT:-prod}
    # The login body goes through stdin (-d @-) and the token through stdin as a header (-H @-), so neither the client
    # secret nor the token appears in the process list. The HTTP status is the reachability fact: no answer, refused, ready.
    RESP=$(printf '{"clientId":"%s","clientSecret":"%s"}' "$SECRET_STORE_CLIENT_ID" "$SECRET_STORE_CLIENT_SECRET" \
           | curl -s -m 15 -w '\n%{http_code}' "$SECRET_STORE_DOMAIN/api/v1/auth/universal-auth/login" \
                  -H 'Content-Type: application/json' -d @-) || RESP=$'\n000'
    CODE=${RESP##*$'\n'}
    case "$CODE" in
      200) T=$(printf '%s' "${RESP%$'\n'*}" | jq -er '.accessToken | strings' 2>/dev/null) || T="" ;;
      000) echo "source: secret store not reachable (no answer from $SECRET_STORE_DOMAIN within 15 s) - keeping the existing $OUT"; finish kept ;;
      400|401|403) echo "source: secret store refused the login (HTTP $CODE; check the client id and secret) - keeping the existing $OUT"; finish kept ;;
      *) echo "source: secret store answered the login with HTTP $CODE - keeping the existing $OUT"; finish kept ;;
    esac
    [ -n "$T" ] || { echo "source: secret store answered the login without an access token - keeping the existing $OUT"; finish kept; }
    echo "source: secret store ready"
    fetch() {
      local resp code
      VALUE=""; DETAIL=""
      resp=$(printf 'Authorization: Bearer %s\n' "$T" \
             | curl -s -m 15 -w '\n%{http_code}' -H @- \
                    "$SECRET_STORE_DOMAIN/api/v3/secrets/raw/$1?workspaceId=$PROJECT_ID&environment=$ENVIRONMENT&secretPath=%2F") \
        || resp=$'\n000'
      code=${resp##*$'\n'}
      case "$code" in
        200) if VALUE=$(printf '%s' "${resp%$'\n'*}" | jq -er '.secret.secretValue | strings' 2>/dev/null); then FOUND=present
             else FOUND=failed; DETAIL="HTTP 200 without a secret value"; fi ;;
        404) FOUND=missing ;;
        000) FOUND=failed; DETAIL="no answer" ;;
        *) FOUND=failed; DETAIL="HTTP $code" ;;
      esac
    }
    ;;
  file)
    [ -n "$SECRET_FILE" ] || { echo "SECRET_SOURCE=file needs SECRET_FILE in render-env.conf - nothing rendered"; finish refused; }
    [ -e "$SECRET_FILE" ] || { echo "source: $SECRET_FILE missing - keeping the existing $OUT"; finish kept; }
    not_router_writable "$SECRET_FILE" || finish refused
    SRC=$(readlink -f -- "$SECRET_FILE")
    [ -f "$SRC" ] || { echo "$SECRET_FILE is not a regular file - not read"; finish refused; }
    [ $(( 0$(stat -c %a -- "$SRC") & 077 )) -eq 0 ] || { echo "$SECRET_FILE is readable by group or others (it holds secrets: chmod 600) - not read"; finish refused; }
    # KEY=value per line, the value raw up to the end of the line (the format of secrets.env). Comments (#) and blank lines
    # are skipped. Only rendered names are taken; a name given twice is ambiguous and taken from neither line.
    declare -A FILE_VALUES=() FILE_TWICE=()
    N=0
    while IFS= read -r LINE || [ -n "$LINE" ]; do
      N=$((N + 1))
      case "$LINE" in ''|'#'*) continue ;; esac
      NAME=${LINE%%=*}
      [ "$NAME" != "$LINE" ] || { echo "$SECRET_FILE line $N: not KEY=value - skipped"; continue; }
      case "$NAME" in
        ''|[!A-Z]*|*[!A-Z0-9_]*) echo "$SECRET_FILE line $N: not a valid name - skipped"; continue ;;
      esac
      is_key "$NAME" || { echo "$SECRET_FILE line $N: $NAME is neither built in nor in EXTRA_SECRET_KEYS - skipped"; continue; }
      if [ -n "${FILE_VALUES[$NAME]+set}" ]; then FILE_TWICE[$NAME]=1; fi
      FILE_VALUES[$NAME]=${LINE#*=}
    done < "$SRC"
    for NAME in "${!FILE_TWICE[@]}"; do
      echo "$SECRET_FILE: $NAME is given more than once - skipped"; unset "FILE_VALUES[$NAME]"
    done
    echo "source: $SECRET_FILE ready"
    fetch() {
      VALUE=""; DETAIL=""
      if [ -n "${FILE_VALUES[$1]+set}" ]; then VALUE=${FILE_VALUES[$1]}; FOUND=present; else FOUND=missing; fi
    }
    ;;
  *)
    echo "SECRET_SOURCE '$SECRET_SOURCE' is unknown (store or file) - nothing rendered"; finish refused ;;
esac

# --- step 2: every name on its own ------------------------------------------------------------------------------------
# A missing name is reported by name, never a value. An empty value counts as missing.
LINES=""; PRESENT=0; FAILED=""; TOTAL=0
for K in $KEYS; do
  TOTAL=$((TOTAL + 1))
  fetch "$K"
  case "$FOUND" in
    failed) echo "$K: fetch failed ($DETAIL)"; FAILED="$FAILED $K"; continue ;;
    missing) echo "$K: missing in the source - not rendered"; continue ;;
  esac
  [ -n "$VALUE" ] || { echo "$K: empty in the source - not rendered"; continue; }
  one_line "$VALUE" || { echo "$K contains a line break - skipped"; continue; }
  LINES="$LINES$K=$VALUE"$'\n'
  PRESENT=$((PRESENT + 1))
done
[ -z "$FAILED" ] || { echo "fetch failed for:$FAILED - keeping the existing $OUT, so no secret is dropped"; finish kept; }
[ "$PRESENT" -gt 0 ] || { echo "the source holds none of the $TOTAL names (check PROJECT_ID, ENVIRONMENT or SECRET_FILE) - keeping the existing $OUT"; finish kept; }

# --- step 3: render ---------------------------------------------------------------------------------------------------
# temporary file next to the target, so the final mv is a rename in one folder (atomic, never follows a link)
TMP=$(mktemp "$(dirname "$OUT")/.secrets.env.XXXXXX")
trap 'rm -f "$TMP"' EXIT
chmod 600 "$TMP"; printf '%s' "$LINES" > "$TMP"
# the router's account reads it through its group (ADR-0086); without the account (older layout) it stays root's 0600
if getent group skirnir-router >/dev/null 2>&1; then chgrp skirnir-router "$TMP" && chmod 640 "$TMP"; fi
if [ ! -f "$OUT" ] || ! cmp -s "$TMP" "$OUT"; then
  mv "$TMP" "$OUT"; echo "$(date) $OUT updated: $PRESENT of $TOTAL names"
  if [ "$RESTART" = 1 ] && systemctl is-active --quiet skirnir-router; then
    systemctl restart --no-block skirnir-router && echo "skirnir-router restart requested (secrets changed)"
  fi
  finish written
fi
echo "$(date) $OUT unchanged: $PRESENT of $TOTAL names"
finish unchanged
