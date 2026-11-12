#!/bin/bash
# Sets up the router's own account and its folders (ADR-0086). Root tool, idempotent: install.sh and deploy.py run it after
# copying the files, skirnir-router-cert.service runs it with --tls after a certificate renewal.
#
#   router-layout.sh            account, owners and modes; moves the router's own files out of the configuration folder
#   router-layout.sh --tls      only the certificate folders certs/ and tls/: readable for the account's group
#   router-layout.sh --check    report what would change, change nothing
#
# Layout: /etc/skirnir-router is root:skirnir-router 0750 - config.yaml and everything else there is written by root
# tools and read by the router through its group, writable for nobody else; certificate keys 0640. The files the router
# writes itself live in /var/lib/skirnir-router (router.state_dir in config.yaml), its logs in /var/log/skirnir-router,
# both owned by the account. secrets.env stays in /etc/skirnir-router, root:skirnir-router 0640 (environment and the
# raw-read mqtt.password_file); now that the router cannot write that folder, render-env.sh may write there safely.
#
# Checks before acting: root; config.yaml (if present) names that state folder in router.state_dir; the configuration
# folder and the certificate folders are real folders owned by root - otherwise a root tool would change owners wherever
# someone pointed it. Symlinks the router may have left in its old writable folder are removed, never followed.
# For tests: --root DIR works below DIR without root and without changing owners. Last line: STATE: done | checked | refused.
set -u
ACCOUNT=skirnir-router
CONFIG_DIR=/etc/skirnir-router
STATE_DIR=/var/lib/skirnir-router
LOG_DIR=/var/log/skirnir-router
ROOT=""
MODE=all
# Every file the router writes next to config.yaml before ADR-0086 (rotations included); requests.jsonl is the decision
# observation (anfragen.jsonl before router 0.9.0).
STATE_FILES="roles.yaml nodes.json deletions.jsonl events.jsonl usage.json metrics.json perf.json ollama-latest.json
             requests.jsonl requests.jsonl.1 anfragen.jsonl anfragen.jsonl.1"
refuse() { echo "$1"; echo "STATE: refused"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --tls) MODE=tls; shift ;;
    --check) MODE=check; shift ;;
    --root) ROOT="${2:?--root needs a folder}"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) refuse "unknown option $1 (see --help)" ;;
  esac
done
C="$ROOT$CONFIG_DIR"; S="$ROOT$STATE_DIR"; L="$ROOT$LOG_DIR"
OWNERS=1
[ -n "$ROOT" ] && OWNERS=0
if [ -z "$ROOT" ] && [ "$(id -u)" -ne 0 ]; then refuse "Run as root (sudo bash $0)."; fi
CHANGED=0
act() {   # act <description> <command...>: report; run unless --check
  echo "$1"
  CHANGED=1
  [ "$MODE" = check ] && return 0
  shift
  "$@" || refuse "failed: $*"
}

# A real folder (no symlink), owned by root - or missing.
root_folder() {
  [ -L "$1" ] && return 1
  [ -e "$1" ] || return 0
  [ -d "$1" ] || return 1
  [ "$OWNERS" = 0 ] || [ "$(stat -c %u "$1")" = 0 ]
}
root_folder "$C" || refuse "$C is a symlink, not a folder, or not owned by root - fix that by hand first."

# ---- certificate folders (also --tls) ----------------------------------------------------------------------------------
tls_folders() {
  local d f
  for d in "$C/certs" "$C/tls"; do
    [ -e "$d" ] || [ -L "$d" ] || continue
    root_folder "$d" || refuse "$d is a symlink, not a folder, or not owned by root - fix that by hand first."
    if [ "$OWNERS" = 1 ] && { [ "$(stat -c %g "$d")" != "$(getent group "$ACCOUNT" | cut -d: -f3)" ] \
                               || [ "$(stat -c %a "$d")" != 750 ]; }; then
      act "$d: group $ACCOUNT, 0750" sh -c "chgrp $ACCOUNT '$d' && chmod 0750 '$d'"
    fi
    for f in "$d"/*; do
      [ -e "$f" ] || [ -L "$f" ] || continue
      if [ -L "$f" ]; then
        echo "$f is a symlink, left as it is: its target must be readable for group $ACCOUNT"
        continue
      fi
      [ -f "$f" ] || continue
      [ "$OWNERS" = 1 ] || continue
      # group may read, group may not write, others nothing - compared as numbers ($((...)) reads the leading 0 as octal)
      if [ "$(stat -c %U:%G "$f")" != "root:$ACCOUNT" ] || [ $(( 0$(stat -c %a "$f") & 047 )) -ne $(( 040 )) ]; then
        act "$f: root:$ACCOUNT, readable for the group only" sh -c "chown root:$ACCOUNT '$f' && chmod g+r,g-w,o-rwx '$f'"
      fi
    done
  done
}

if [ "$MODE" = tls ]; then
  getent group "$ACCOUNT" >/dev/null || refuse "group $ACCOUNT does not exist yet - run router-layout.sh without --tls first."
  tls_folders
  echo "STATE: done"
  exit 0
fi

# ---- configuration must name the state folder ---------------------------------------------------------------------------
if [ -f "$C/config.yaml" ]; then
  SD=$(python3 -c 'import sys, yaml
c = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print((c.get("router") or {}).get("state_dir") or "")' "$C/config.yaml") || refuse "$C/config.yaml cannot be read as YAML."
  [ "$SD" = "$STATE_DIR" ] || refuse "$C/config.yaml must set router.state_dir: $STATE_DIR (found: '${SD:-unset}'); the router can no longer write next to config.yaml (ADR-0086)."
fi

# ---- account -----------------------------------------------------------------------------------------------------------
if [ "$OWNERS" = 1 ] && ! getent passwd "$ACCOUNT" >/dev/null; then
  act "creating system account $ACCOUNT" useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home \
    --shell /usr/sbin/nologin "$ACCOUNT"
fi

# ---- the router's own files: out of the configuration folder -------------------------------------------------------------
# A router from before still runs as root and saves its files next to config.yaml when it stops - after the move, so the
# newer state would land in the old place. It is stopped first; the caller starts the new version (deploy.py restarts it,
# install.sh says so). Its unit is the new one on disk already, so only the running process is old.
PENDING=""
for f in $STATE_FILES; do [ -e "$C/$f" ] || [ -L "$C/$f" ] && PENDING="$PENDING $f"; done
if [ -n "$PENDING" ] && [ "$OWNERS" = 1 ] && systemctl is-active -q skirnir-router 2>/dev/null; then
  PID=$(systemctl show -p MainPID --value skirnir-router)
  if [ "$PID" != 0 ] && [ "$(stat -c %u "/proc/$PID")" = 0 ]; then
    act "stopping the router that still runs as root, so it saves its files before they move (start it again afterwards)" \
      systemctl stop skirnir-router
    [ "$MODE" = check ] || echo "ROUTER: stopped"
    PENDING=""
    for f in $STATE_FILES; do [ -e "$C/$f" ] || [ -L "$C/$f" ] && PENDING="$PENDING $f"; done
  fi
fi
[ -d "$S" ] || act "creating $S" mkdir -p "$S"
for f in $STATE_FILES; do
  if [ -L "$C/$f" ]; then
    act "$C/$f is a symlink (the router could write that folder before) - removed, not followed" rm -f "$C/$f"
  elif [ -f "$C/$f" ]; then
    if [ -e "$S/$f" ] || [ -L "$S/$f" ]; then
      echo "$S/$f exists already; $C/$f is no longer used (it stays root's)"
    else
      act "moving $C/$f -> $S/$f" mv "$C/$f" "$S/$f"
    fi
  fi
done
if [ "$OWNERS" = 1 ]; then
  for d in "$S" "$L"; do
    [ -d "$d" ] || continue
    if [ -n "$(find "$d" \! -user "$ACCOUNT" -print -quit 2>/dev/null)" ] || [ "$(stat -c %a "$d")" != 700 ]; then
      act "$d: owned by $ACCOUNT, 0700" sh -c "chown -R -h $ACCOUNT:$ACCOUNT '$d' && chmod 0700 '$d'"
    fi
  done
fi

# ---- secrets.env: stays, root's, readable for the group (the router also reads it as a file: mqtt.password_file) ---------
# render-env.sh writes it again; a symlink there can only come from the time the router could write this folder.
if [ -L "$C/secrets.env" ]; then
  act "$C/secrets.env is a symlink (the router could write that folder before) - removed, not followed; render-env.sh writes it anew" \
    rm -f "$C/secrets.env"
fi

# ---- configuration folder: root's, readable through the group ------------------------------------------------------------
if [ -d "$C" ] && [ "$OWNERS" = 1 ]; then
  if [ -n "$(find "$C" \( \! -user root -o \! -group "$ACCOUNT" -o -perm /0022 \) \! -type l -print -quit)" ] \
     || [ "$(stat -c %a "$C")" != 750 ]; then
    act "$C: root:$ACCOUNT, writable for root only, 0750" \
      sh -c "chown -R -h root:$ACCOUNT '$C' && chmod -R g+rX,g-w,o-w '$C' && chmod 0750 '$C'"
  fi
  for f in config.yaml secrets.env; do
    if [ -f "$C/$f" ] && [ ! -L "$C/$f" ] && [ "$(stat -c %a "$C/$f")" != 640 ]; then
      act "$C/$f: 0640" chmod 0640 "$C/$f"
    fi
  done
fi
tls_folders

[ "$CHANGED" = 0 ] && echo "layout is current"
[ "$MODE" = check ] && { echo "STATE: checked"; exit 0; }
echo "STATE: done"
