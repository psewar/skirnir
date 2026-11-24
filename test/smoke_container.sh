#!/bin/bash
# Smoke test of the router image (ADR-0116): build it, check the example configuration inside it, write a first
# configuration with --init into a mounted folder (the documented first start), start it as compose.yaml does and check
# health, the API from outside, the unprivileged account, the read-only configuration and the start's posture report.
#
#   bash test/smoke_container.sh [--engine docker|podman] [--image NAME] [--no-build] [--selftests]
#
# --selftests runs, instead of the start, the router's self-tests from the CI python job inside the image (its Python
# and packages, the source mounted read-only, as uid 10001 on a read-only root), except the ones that test repository
# tooling and need git, pip or Go (TOOLING below). CI runs the router's tests on Python 3.11; this covers the image's.
#
# Needs the engine (rootful: the test reads owners of files the container wrote) and curl. Works in a fresh temporary
# folder with a self-signed certificate for the reserved name router.example.net (test/smoke_container_cert.py) and a
# random container name; removes container, volumes and folder at the end. Pushes nothing anywhere. Last line:
# STATE: passed | failed | refused.
set -u
ENGINE=""
IMAGE=skirnir-router-smoke
BUILD=1
SELFTESTS=0
# Self-tests of repository tooling, not of the router: they need git, pip, Go or the lock tool, which the image lacks.
TOOLING="selftest_pinned_references selftest_pydeps selftest_sbom selftest_threat_review selftest_trusted_gate
         selftest_dependency_audit selftest_container_image selftest_control_flow selftest_english_names
         selftest_training_data selftest_privacy_threshold"
refuse() { echo "$1"; echo "STATE: refused"; exit 2; }
# A command that a refusal suggests is meant to be pasted: it repeats only shell-inert text, the rest as a placeholder.
inert() { case "$1" in "" | *[!A-Za-z0-9/._+:@=-]*) return 1 ;; esac; }
ARGS=""
for a in "$@"; do if inert "$a"; then ARGS="$ARGS $a"; else ARGS="$ARGS <value>"; fi; done
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="${2:?--engine needs docker or podman}"; shift 2 ;;
    --image) IMAGE="${2:?--image needs a name}"; shift 2 ;;
    --no-build) BUILD=0; shift ;;
    --selftests) SELFTESTS=1; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) refuse "unknown option $1 (see --help)" ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# --- preconditions ----------------------------------------------------------------------------------------------------
# Git Bash (MSYS2) and Cygwin rewrite arguments that look like POSIX paths before the engine sees them
# (/etc/skirnir-router becomes C:\Program Files\Git\etc\skirnir-router); without that rewriting (MSYS_NO_PATHCONV) the
# engine would not find the host folders under /tmp either. The build would pass and every later step fail with a
# misleading message, so refuse before anything is built or started.
SYSTEM=$(uname -s)
case "$SYSTEM" in
  *_NT-*|MINGW*|MSYS*|CYGWIN*)
    dir=$(printf '%s\n' "$ROOT" | sed -nE 's#^(/cygdrive)?/([a-zA-Z])(/.*)?$#/mnt/\L\2\E\3#p')   # this checkout in WSL
    if inert "${dir// /}"; then dir="\"$dir\""; else dir="/mnt/c/<checkout>"; fi
    refuse "this shell is $SYSTEM (Git Bash, MSYS2 or Cygwin on Windows): it rewrites the container paths this test
passes, so only the build would work. Run it on Linux or inside WSL (Docker Desktop's distribution or the rootful Podman
machine), e.g. from this shell:
  MSYS_NO_PATHCONV=1 wsl -d podman-machine-default -u root -e bash -c 'cd $dir && bash test/smoke_container.sh$ARGS'" ;;
esac
if [ -z "$ENGINE" ]; then
  for e in docker podman; do command -v "$e" >/dev/null 2>&1 && { ENGINE=$e; break; }; done
fi
case "$ENGINE" in docker|podman) ;; *) refuse "no container engine: install docker or podman, or name one with --engine";; esac
"$ENGINE" version >/dev/null 2>&1 || refuse "$ENGINE does not answer ($ENGINE version failed): is its service running?"
command -v curl >/dev/null 2>&1 || refuse "curl is missing"
[ -f "$ROOT/router/Dockerfile" ] || refuse "router/Dockerfile not found below $ROOT"
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/router/skirnir_router/common.py")
[ -n "$VERSION" ] || refuse "no VERSION in router/skirnir_router/common.py"

FAILS=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; FAILS=$((FAILS + 1)); }
expect() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (expected '$2', got '$1')"; fi; }

NAME="skirnir-smoke-$$-$RANDOM"
WORK="$(mktemp -d)" || refuse "cannot create a temporary folder"
cleanup() {
  "$ENGINE" rm -f "$NAME" >/dev/null 2>&1
  "$ENGINE" volume rm -f "$NAME-state" "$NAME-logs" >/dev/null 2>&1
  rm -rf "$WORK" 2>/dev/null || "$ENGINE" run --rm --user 0:0 -v "$WORK:/w" --entrypoint sh "$IMAGE" -c 'rm -rf /w/*'
  rm -rf "$WORK"
}
trap cleanup EXIT
chmod 0755 "$WORK"

# --- build --------------------------------------------------------------------------------------------------------------
if [ "$BUILD" = 1 ]; then
  FORMAT=()
  [ "$ENGINE" = podman ] && FORMAT=(--format docker)   # podman's default OCI format drops HEALTHCHECK
  "$ENGINE" build "${FORMAT[@]}" -t "$IMAGE" "$ROOT/router" > "$WORK/build.log" 2>&1 || { tail -40 "$WORK/build.log"; refuse "the image does not build"; }
  ok "image builds ($ENGINE)"
fi
expect "$("$ENGINE" image inspect -f '{{.Config.User}}' "$IMAGE")" "10001:10001" "image runs as uid/gid 10001"
env_names=$("$ENGINE" image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$IMAGE" | cut -d= -f1 | tr '\n' ' ')
case "$env_names" in
  *PASSWORD*|*TOKEN*|*SECRET*|*DSN*|*API_KEY*) bad "image environment names a secret: $env_names" ;;
  *) ok "no secret in the image environment ($env_names)" ;;
esac

# --- router self-tests on the image's Python ------------------------------------------------------------------------------
if [ "$SELFTESTS" = 1 ]; then
  TESTS=()
  for t in $(grep -oE 'python test/selftest_[a-z0-9_]+\.py' "$ROOT/.github/workflows/ci.yml" | awk '{print $2}' | sort -u); do
    case " $(echo $TOOLING) " in *" $(basename "$t" .py) "*) continue ;; esac
    TESTS+=("$t")
  done
  [ "${#TESTS[@]}" -gt 0 ] || refuse "no self-tests found in .github/workflows/ci.yml"
  "$ENGINE" run --rm --user 10001:10001 --read-only --tmpfs /tmp:size=256m,mode=1777 -v "$ROOT:/src:ro" -w /src \
    --entrypoint sh "$IMAGE" -c 'for t in "$@"; do
        if python "$t" > /tmp/out 2>&1; then echo "SELFTEST ok $t"; else tail -15 /tmp/out; echo "SELFTEST FAILED $t"; fi
      done' sh "${TESTS[@]}" > "$WORK/selftests.log" 2>&1
  grep -v '^SELFTEST ok' "$WORK/selftests.log"
  passed=$(grep -c '^SELFTEST ok' "$WORK/selftests.log")
  failed=$(grep -c '^SELFTEST FAILED' "$WORK/selftests.log")
  [ "$failed" = 0 ] && [ "$passed" = "${#TESTS[@]}" ] && ok "$passed router self-tests pass inside the image" \
    || bad "router self-tests inside the image: $passed of ${#TESTS[@]} passed, $failed failed"
  echo "$FAILS failure(s)"
  [ "$FAILS" = 0 ] && { echo "STATE: passed"; exit 0; }
  echo "STATE: failed"
  exit 1
fi

# --- the example configuration and the first configuration ----------------------------------------------------------------
out=$("$ENGINE" run --rm "$IMAGE" --check /opt/skirnir-router/config.example.yaml --pure 2>&1)
case "$out" in "configuration ok:"*) ok "router.py --check of config.example.yaml inside the image" ;; *) bad "--check: $out" ;; esac
mkdir -m 0755 "$WORK/config"
printf 'smoke-test-only-password\n' | "$ENGINE" run --rm -i --user 0:0 -v "$WORK/config:/etc/skirnir-router" "$IMAGE" \
  --init /etc/skirnir-router --public-url https://router.example.net:11435 > "$WORK/init.log" 2>&1
grep -q '^INIT: written$' "$WORK/init.log" && ok "--init writes config.yaml into the mounted folder" \
  || { grep -v '^  ' "$WORK/init.log"; bad "--init did not write a configuration"; }
expect "$(stat -c '%u:%g %a' "$WORK/config/config.yaml" 2>/dev/null)" "0:10001 640" "config.yaml is root's, readable for the router's group"

# Certificate at the paths --init wrote, group and mode as router-layout.sh --tls sets them on a host; made inside the
# image with its cryptography package, so the host needs no openssl.
"$ENGINE" run --rm -i --user 0:0 -v "$WORK/config:/etc/skirnir-router" --entrypoint python "$IMAGE" - \
  < "$ROOT/test/smoke_container_cert.py" || refuse "cannot write the test certificate"

# --- start as compose.yaml does -------------------------------------------------------------------------------------------
"$ENGINE" run -d --name "$NAME" --user 10001:10001 --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:size=16m,mode=1777 -v "$WORK/config:/etc/skirnir-router:ro" -v "$NAME-state:/var/lib/skirnir-router" \
  -v "$NAME-logs:/var/log/skirnir-router" -p 127.0.0.1::11434 "$IMAGE" > /dev/null || refuse "the container does not start"
healthy=""
for _ in $(seq 1 30); do
  health=$("$ENGINE" exec "$NAME" python /opt/skirnir-router/container/healthcheck.py 2>&1) && { healthy=1; break; }
  sleep 2
done
if [ -n "$healthy" ]; then ok "healthcheck.py inside the container: $health"; else
  "$ENGINE" logs "$NAME" 2>&1 | tail -30; bad "not healthy after 60 s: $health"; fi
PORT=$("$ENGINE" port "$NAME" 11434/tcp | head -1 | sed 's/.*://')
answer=$(curl -sSk --max-time 5 "https://127.0.0.1:$PORT/api/version" 2>&1)
case "$answer" in
  *"router$VERSION\""*) ok "GET /api/version over TLS from outside: $answer" ;;
  *) bad "/api/version on published port '$PORT': '$answer'" ;;
esac
expect "$("$ENGINE" exec "$NAME" id -u)" "10001" "the router runs as uid 10001"
expect "$("$ENGINE" exec "$NAME" sh -c 'grep ^CapEff /proc/1/status | cut -f2')" "0000000000000000" "the router has no capabilities"
"$ENGINE" exec "$NAME" sh -c 'touch /etc/skirnir-router/probe' 2>/dev/null && bad "the configuration folder is writable" \
  || ok "the configuration folder is read-only"
"$ENGINE" exec "$NAME" sh -c 'touch /opt/skirnir-router/probe' 2>/dev/null && bad "the program folder is writable" \
  || ok "the program folder is read-only"
"$ENGINE" exec "$NAME" sh -c 'touch /var/lib/skirnir-router/probe && rm /var/lib/skirnir-router/probe' 2>/dev/null \
  && ok "router.state_dir is writable" || bad "router.state_dir is not writable"

# --- posture at start: facts of this configuration, nothing the container itself causes ----------------------------------------
"$ENGINE" logs "$NAME" > "$WORK/router.log" 2>&1
grep -E 'posture |POSTURE:' "$WORK/router.log" | sed 's/^[^ ]* [^ ]* /  /'
grep -q 'POSTURE:' "$WORK/router.log" && ok "the start reports its posture" || bad "no posture report at start"
grep -q 'tls_files_changeable' "$WORK/router.log" && bad "posture: certificate reported as changeable on a read-only mount" \
  || ok "posture: the read-only certificate is not reported as changeable"
grep -E ' (ERROR|CRITICAL) ' "$WORK/router.log" && bad "errors in the router log" || ok "no errors in the router log"

echo "$FAILS failure(s)"
[ "$FAILS" = 0 ] && { echo "STATE: passed"; exit 0; }
echo "STATE: failed"
exit 1
