#!/bin/bash
# Install or upgrade the Skirnir router on Debian or Ubuntu (ADR-0069). Run from the router/ folder of a release or clone:
#
#   sudo bash router/install.sh             install or upgrade: packages, files, venv, systemd units, checks
#   sudo bash router/install.sh --check     only report what is missing or wrong, change nothing
#
# Options: --prefix DIR (default /opt/skirnir-router), --config-dir DIR (default /etc/skirnir-router).
# For packaging and tests: --root DIR installs below DIR without root, apt or systemctl; --no-venv skips the venv
# (checks then use the system python); --os-release FILE reads the distribution from FILE.
#
# Checks before changing anything: root, Debian/Ubuntu, Python >= 3.11, a complete source folder. Missing packages are
# installed with apt (or reported with --check). The program files are replaced, the configuration is never touched:
# without one, the next step is `router.py --init`; with one, it is checked like the service will read it (--check
# --files without capabilities, then --posture). The last line is always STATE: installed | checked | refused.
set -u

PREFIX=/opt/skirnir-router
CONFIG_DIR=/etc/skirnir-router
ROOT=""
CHECK_ONLY=0
NO_VENV=0
OS_RELEASE=/etc/os-release
PACKAGES="python3 python3-venv python3-aiohttp python3-yaml python3-cryptography python3-paho-mqtt python3-uvloop curl jq"

refuse() { echo "$1"; echo "STATE: refused"; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a folder}"; shift 2 ;;
    --config-dir) CONFIG_DIR="${2:?--config-dir needs a folder}"; shift 2 ;;
    --root) ROOT="${2:?--root needs a folder}"; shift 2 ;;
    --check) CHECK_ONLY=1; shift ;;
    --no-venv) NO_VENV=1; shift ;;
    --os-release) OS_RELEASE="${2:?--os-release needs a file}"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) refuse "unknown option $1 (see --help)" ;;
  esac
done

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$ROOT$PREFIX"
CONF="$ROOT$CONFIG_DIR"
UNITS="$ROOT/etc/systemd/system"
VENV="$DEST/venv"

# --- checks before any change ---------------------------------------------------------------------------------
if [ -z "$ROOT" ] && [ "$(id -u)" -ne 0 ]; then
  refuse "Run as root (sudo bash $0), or use --root DIR for a staging install."
fi
[ -r "$OS_RELEASE" ] || refuse "Cannot read $OS_RELEASE: unknown distribution."
OS_IDS="$(. "$OS_RELEASE"; echo "${ID:-} ${ID_LIKE:-}")"
case " $OS_IDS " in
  *" debian "*|*" ubuntu "*) ;;
  *) refuse "Supported: Debian and Ubuntu (found: ${OS_IDS% }). Other systems: follow docs/install-router.md by hand." ;;
esac
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || refuse "Python 3.11 or newer is required (found $(python3 -V 2>&1))."
elif [ -n "$ROOT" ] || [ "$CHECK_ONLY" -eq 1 ]; then
  refuse "python3 is missing."
fi
for f in router.py skirnir_router/app.py skirnir-router.service router-layout.sh config.minimal.yaml requirements.txt requirements-session.txt ui.html; do
  [ -e "$SRC/$f" ] || refuse "Incomplete source folder $SRC: $f is missing. Run install.sh from the router/ folder of a release."
done
# ADR-0086: the new unit runs the router under its own account and needs router.state_dir in an existing configuration.
# Checked before anything changes, so a refusal leaves program files and units as they were.
PRE_LAYOUT=(bash "$SRC/router-layout.sh" --check)
[ -n "$ROOT" ] && PRE_LAYOUT+=(--root "$ROOT")
if [ -f "$CONF/config.yaml" ]; then
  PRE_OUT="$("${PRE_LAYOUT[@]}" 2>&1)" || { echo "$PRE_OUT" | grep -v '^STATE:'; refuse "Nothing was changed."; }
fi

# --- packages ---------------------------------------------------------------------------------------------------
MISSING=""
if command -v dpkg-query >/dev/null 2>&1; then
  for p in $PACKAGES; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || MISSING="$MISSING $p"
  done
fi
if [ -n "$MISSING" ]; then
  if [ "$CHECK_ONLY" -eq 1 ] || [ -n "$ROOT" ]; then
    echo "Missing packages:$MISSING (installed by a normal run)"
  else
    echo "Installing packages:$MISSING"
    apt-get update -q >/dev/null && apt-get install -y -q $MISSING >/dev/null || refuse "apt-get could not install:$MISSING"
  fi
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  [ -f "$DEST/router.py" ] && echo "Installed: $DEST ($(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DEST/skirnir_router/common.py"))" \
    || echo "Not installed yet: $DEST"
  [ -f "$CONF/config.yaml" ] && echo "Configuration: $CONF/config.yaml" || echo "No configuration yet: $CONF/config.yaml"
  echo "STATE: checked"
  exit 0
fi

# --- files ------------------------------------------------------------------------------------------------------
mkdir -p "$DEST" "$UNITS" || refuse "cannot create $DEST or $UNITS"
STAGE="$(mktemp -d "$DEST/.install.XXXXXX")" || refuse "cannot stage in $DEST"
cp -R "$SRC/skirnir_router" "$STAGE/skirnir_router" && find "$STAGE/skirnir_router" -name '__pycache__' -prune -exec rm -rf {} + \
  || refuse "copying the package failed"
for f in router.py ui.html skirnir.png favicon.png config.example.yaml config.minimal.yaml decision-tfidf.json \
         render-env.sh router-layout.sh render-env.conf.example requirements.txt requirements-session.txt requirements-lock.txt install.sh; do
  [ -e "$SRC/$f" ] && cp "$SRC/$f" "$STAGE/$f"
done
rm -rf "$DEST/skirnir_router.old"
[ -d "$DEST/skirnir_router" ] && mv "$DEST/skirnir_router" "$DEST/skirnir_router.old"
mv "$STAGE/skirnir_router" "$DEST/skirnir_router" || refuse "replacing the package failed"
for f in "$STAGE"/*; do mv -f "$f" "$DEST/"; done
rm -rf "$STAGE" "$DEST/skirnir_router.old"
for u in "$SRC"/skirnir-router*.service "$SRC"/skirnir-router*.path "$SRC"/skirnir-router*.timer; do
  [ -e "$u" ] && cp "$u" "$UNITS/"
done
mkdir -p "$CONF" && chmod 0750 "$CONF" || refuse "cannot create $CONF"
# Secret-store access for the root renderers lives outside what the router may write (ADR-0073); move an old copy there.
SECRETS="$ROOT/etc/skirnir-router-secrets"
mkdir -p "$SECRETS" && chmod 0700 "$SECRETS" || refuse "cannot create $SECRETS"
if [ -e "$CONF/render-env.conf" ]; then
  if [ -e "$SECRETS/render-env.conf" ]; then
    rm -f "$CONF/render-env.conf" && echo "Removed $CONF/render-env.conf (the copy in $SECRETS is used)"
  else
    mv "$CONF/render-env.conf" "$SECRETS/render-env.conf" && chmod 0600 "$SECRETS/render-env.conf" \
      && echo "Moved render-env.conf to $SECRETS (ADR-0073)" || refuse "cannot move render-env.conf to $SECRETS"
  fi
fi
echo "Program files: $DEST (version $(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DEST/skirnir_router/common.py"))"

# --- venv -------------------------------------------------------------------------------------------------------
PY=python3
if [ "$NO_VENV" -eq 0 ]; then
  [ -x "$VENV/bin/python" ] || python3 -m venv --system-site-packages "$VENV" || refuse "python3 -m venv failed"
  # Only what the distribution lacks, and only from the hash-locked file (ADR-0074): pydeps.py plans, pip checks hashes.
  PLAN="$(mktemp)" || refuse "cannot create a temporary file"
  "$VENV/bin/python" "$DEST/skirnir_router/pydeps.py" plan --requirements "$DEST/requirements-session.txt" \
    --lock "$DEST/requirements-lock.txt" > "$PLAN" \
    || { rm -f "$PLAN"; refuse "the venv needs Python packages that neither the distribution nor requirements-lock.txt provides (above)"; }
  if [ -s "$PLAN" ]; then
    "$VENV/bin/python" -m pip install -q --disable-pip-version-check --require-hashes --no-deps -r "$PLAN" \
      || { rm -f "$PLAN"; refuse "pip could not install the locked packages into $VENV"; }
  fi
  rm -f "$PLAN"
  "$VENV/bin/python" "$DEST/skirnir_router/pydeps.py" verify --requirements "$DEST/requirements-session.txt" \
    || refuse "the venv does not meet router/requirements-session.txt"
  "$VENV/bin/python" -c 'import aiohttp, yaml, cryptography, paho.mqtt' || refuse "the venv cannot import the router's packages"
  PY="$VENV/bin/python"
  echo "Python environment: $VENV"
fi

# --- systemd and checks -----------------------------------------------------------------------------------------
if [ -z "$ROOT" ]; then
  systemctl daemon-reload || refuse "systemctl daemon-reload failed"
fi
# The service account and the folders it owns (ADR-0086): account, owners and modes, the router's own files moved out of
# the configuration folder (a router still running as root is stopped first, so it saves them before the move).
LAYOUT=(bash "$DEST/router-layout.sh")
[ -n "$ROOT" ] && LAYOUT+=(--root "$ROOT")
LAYOUT_OUT="$("${LAYOUT[@]}" 2>&1)"
LAYOUT_RC=$?
echo "$LAYOUT_OUT" | grep -v '^ROUTER: '
[ "$LAYOUT_RC" -eq 0 ] || refuse "The service account layout above is not in place; the program files are installed."
if [ -f "$CONF/config.yaml" ]; then
  CHECK=("$PY" "$DEST/router.py" --check "$CONF/config.yaml" --files)
  # As the service reads it: its own account without capabilities (ADR-0044, ADR-0086) - a file the account cannot
  # read fails here.
  if [ -z "$ROOT" ] && command -v setpriv >/dev/null 2>&1; then
    CHECK=(setpriv --reuid=skirnir-router --regid=skirnir-router --init-groups --inh-caps=-all --bounding-set=-all "${CHECK[@]}")
  fi
  "${CHECK[@]}" || refuse "The configuration $CONF/config.yaml does not pass the check above; the program files are installed, the service was not touched."
  "$PY" "$DEST/router.py" --posture "$CONF/config.yaml"
  if echo "$LAYOUT_OUT" | grep -q '^ROUTER: stopped'; then
    echo "Next: systemctl start skirnir-router   (stopped above for the move to its own account)"
  elif [ -z "$ROOT" ] && systemctl is-active -q skirnir-router; then
    echo "Next: systemctl restart skirnir-router   (the running router still has the previous version)"
  else
    echo "Next: systemctl enable --now skirnir-router"
  fi
else
  echo "No configuration yet. Next:"
  echo "  $PY $DEST/router.py --init $CONFIG_DIR --public-url https://<router-name>:11435"
  echo "  then put the TLS certificate and key at the paths in control_tls, run bash $DEST/router-layout.sh again (their"
  echo "  group and mode for the router's account), and: systemctl enable --now skirnir-router"
fi
echo "STATE: installed"
