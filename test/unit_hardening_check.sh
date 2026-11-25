#!/bin/bash
# Run the router under its real systemd unit (router/skirnir-router.service) and check that the hardening leaves it
# working (ADR-0138, TM-14): a throwaway Debian 13 container with systemd as PID 1, router/install.sh, a synthetic
# configuration with TLS, then the API, the UI, a configuration write and a reload; it fails on a wrong answer or a
# journal line that looks like a refused system call or file access, and prints systemd-analyze's exposure.
#
#   bash test/unit_hardening_check.sh [--engine docker|podman] [--keep]
#
# Needs a rootful engine that may run a privileged container (systemd sets up the unit's namespaces) on Linux or in WSL
# (the rootful Podman machine, Docker Desktop's distribution). Not part of CI. The last line is always
# STATE: passed | failed | refused.
set -u
ENGINE=""; KEEP=0
refuse() { echo "$1"; echo "STATE: refused"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="${2:?--engine needs docker or podman}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) refuse "unknown option $1 (see the header of this script)" ;;
  esac
done
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
case "$(uname -s)" in
  *_NT-*|MINGW*|MSYS*|CYGWIN*)
    refuse "this shell rewrites the container paths this check passes; run it on Linux or inside WSL, e.g.
  MSYS_NO_PATHCONV=1 wsl -d podman-machine-default -u root -e bash -c 'cd /mnt/<drive>/<checkout> && bash test/unit_hardening_check.sh'" ;;
esac
if [ -z "$ENGINE" ]; then
  for e in podman docker; do command -v "$e" >/dev/null 2>&1 && { ENGINE=$e; break; }; done
fi
case "$ENGINE" in docker|podman) ;; *) refuse "no container engine: install podman or docker, or name one with --engine";; esac
[ "$(id -u)" = 0 ] || [ "$ENGINE" = docker ] || refuse "run as root (rootful $ENGINE): systemd needs a privileged container"
IMAGE=skirnir-unit-hardening:local
NAME=skirnir-unit-hardening-$$
"$ENGINE" build -q -t "$IMAGE" -f "$ROOT/test/unit_hardening/Containerfile" "$ROOT/test/unit_hardening" > /dev/null \
  || refuse "building the test image failed (network for the Debian packages?)"
RUN=(run -d --name "$NAME" --privileged -v "$ROOT":/src:ro)
[ "$ENGINE" = podman ] && RUN+=(--systemd=always)
"$ENGINE" "${RUN[@]}" "$IMAGE" > /dev/null || refuse "starting the systemd container failed"
for _ in $(seq 1 30); do
  "$ENGINE" exec "$NAME" systemctl is-system-running 2>/dev/null | grep -q -E 'running|degraded' && break; sleep 1
done
"$ENGINE" exec "$NAME" bash /src/test/unit_hardening/inside.sh
rc=$?
[ "$KEEP" = 1 ] && echo "container kept: $ENGINE exec -it $NAME bash" || "$ENGINE" rm -f "$NAME" > /dev/null
[ "$rc" = 0 ] && echo "STATE: passed" || { echo "STATE: failed"; exit 1; }
