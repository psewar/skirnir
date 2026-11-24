#!/bin/bash
# Build check of the decision service images (ADR-0127). Dependabot updates their base images (ADR-0055) and their
# Dockerfiles install the newest packages, so a base whose Python a package does not support yet only shows when the
# image is built (#150, #173: onnxruntime without a wheel for Python 3.15). Per service: build the image from its
# directory as its compose.yaml does, then, inside it without network, compile every program file in /app on the
# image's Python and load every module they import from outside /app, with the names they take from it. No service code
# runs, so no model files are needed.
#
#   bash test/smoke_decision_images.sh [--engine docker|podman] [SERVICE ...]   (default: every service below)
#
# CI runs it per service (.github/workflows/decision-images.yml). Needs the engine and network for the build; pushes
# nothing and leaves the images as skirnir-<service>-check. Last line: STATE: passed | failed | refused.
set -u
SERVICES_ALL="decision-embed decision-jevlike"
ENGINE=""
SERVICES=""
refuse() { echo "$1"; echo "STATE: refused"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="${2:?--engine needs docker or podman}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) refuse "unknown option $1 (see --help)" ;;
    *) SERVICES="$SERVICES $1"; shift ;;
  esac
done
[ -n "$SERVICES" ] || SERVICES=$SERVICES_ALL

# Runs inside the image: compile the program files, then load what they import (module level and inside functions).
# Exit 0 = everything loads, 1 = a module or name is missing (listed); an import that raises shows its traceback.
CHECK='
import ast, importlib, importlib.util, pathlib, platform, sys
app = pathlib.Path("/app")
files = sorted(app.glob("*.py"))
local = {p.stem for p in files}
wanted = {}
for p in files:
    text = p.read_text(encoding="utf-8")
    compile(text, str(p), "exec")
    for node in ast.walk(ast.parse(text, str(p))):
        if isinstance(node, ast.Import):
            for a in node.names:
                wanted.setdefault(a.name, set())
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module != "__future__":
            wanted.setdefault(node.module, set()).update(a.name for a in node.names)
missing, loaded = [], []
for mod in sorted(m for m in wanted if m.split(".")[0] not in local):
    if importlib.util.find_spec(mod) is None:
        missing.append(mod)
        continue
    module = importlib.import_module(mod)
    for name in sorted(wanted[mod] - {"*"}):
        submodule = hasattr(module, "__path__") and importlib.util.find_spec(mod + "." + name) is not None
        if not hasattr(module, name) and not submodule:
            missing.append(mod + "." + name)
    if mod.split(".")[0] not in sys.stdlib_module_names:
        loaded.append(mod)
print("Python", platform.python_version(), "-", len(files), "program files;", "loaded:", ", ".join(loaded) or "-")
if missing:
    print("missing:", ", ".join(missing))
    sys.exit(1)
'

# --- preconditions ----------------------------------------------------------------------------------------------------
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "$ENGINE" ]; then
  for e in docker podman; do command -v "$e" >/dev/null 2>&1 && { ENGINE=$e; break; }; done
fi
case "$ENGINE" in docker|podman) ;; *) refuse "no container engine: install docker or podman, or name one with --engine";; esac
"$ENGINE" version >/dev/null 2>&1 || refuse "$ENGINE does not answer ($ENGINE version failed): is its service running?"
for s in $SERVICES; do
  case " $SERVICES_ALL " in *" $s "*) ;; *) refuse "unknown service $s (known: $SERVICES_ALL)" ;; esac
  [ -f "$ROOT/$s/Dockerfile" ] || refuse "$s/Dockerfile not found below $ROOT"
done

FAILS=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; FAILS=$((FAILS + 1)); }
LOG="$(mktemp)" || refuse "cannot create a temporary file"
trap 'rm -f "$LOG"' EXIT

# --- per service ----------------------------------------------------------------------------------------------------------
for s in $SERVICES; do
  image="skirnir-$s-check"
  base=$(sed -n 's/^FROM //p' "$ROOT/$s/Dockerfile" | head -1)
  base=${base%%@*}
  if ! "$ENGINE" build -t "$image" "$ROOT/$s" > "$LOG" 2>&1; then
    tail -25 "$LOG"
    bad "$s: the image does not build on $base"
    continue
  fi
  ok "$s: the image builds on $base"
  if "$ENGINE" run --rm --network none --entrypoint python "$image" -c "$CHECK" > "$LOG" 2>&1; then
    ok "$s: program files compile and their imports load ($(tail -1 "$LOG"))"
  else
    tail -15 "$LOG"
    bad "$s: program files do not compile or an import is missing"
  fi
done

if [ "$FAILS" = 0 ]; then echo "STATE: passed"; exit 0; fi
echo "$FAILS failure(s)"
echo "STATE: failed"
exit 1
