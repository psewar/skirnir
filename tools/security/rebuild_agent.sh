#!/bin/bash
# Rebuild a release's published agent binaries and compare them byte for byte (ADR-0075, ADR-0141).
#
#   bash tools/security/rebuild_agent.sh <published dir> <out dir> <agent version> <git ref>...
#
# Tries each git ref in order: exports agent/ of that ref (no working tree, no untracked files), builds the Windows and
# Linux binaries the way the release does (CGO off, -trimpath, -s -w, the version in main.version; the Go toolchain is the
# caller's, which the workflow takes from the published binary), and compares every published binary (skirnir-agent-
# <version>-windows-amd64.exe and -linux-amd64; SBOM files next to them are ignored). The first ref whose builds are all
# identical wins: the rebuilt binaries stay in <out dir> and the last line is "REBUILT FROM <ref>". A release that ships
# the agent of an earlier release (the agent did not change, but agent/ may have, e.g. a dependency bump) is reproduced
# from the release that first published that version. No ref reproduces them: every difference is printed with both
# hashes and the script exits 1; nothing is left in <out dir>.
set -euo pipefail
[ $# -ge 4 ] || { echo "usage: rebuild_agent.sh <published dir> <out dir> <agent version> <git ref>..."; exit 2; }
PUB=$1; OUT=$2; V=$3; shift 3
case "$V" in *[!0-9.]*|"") echo "unexpected agent version '$V'"; exit 2 ;; esac
BINARIES=("skirnir-agent-$V-windows-amd64.exe" "skirnir-agent-$V-linux-amd64")
for n in "${BINARIES[@]}"; do
  [ -f "$PUB/$n" ] || { echo "published binary $PUB/$n is missing"; exit 2; }
done
export GOFLAGS=-mod=mod CGO_ENABLED=0 GOTOOLCHAIN=local
OUT_ABS=$(mkdir -p "$OUT" && cd "$OUT" && pwd)
for ref in "$@"; do
  git rev-parse --verify -q "$ref^{commit}" > /dev/null || { echo "$ref: not a commit in this checkout"; continue; }
  work=$(mktemp -d)
  git archive "$ref" agent | tar -x -C "$work"
  rm -f "$OUT_ABS"/skirnir-agent-*
  (cd "$work/agent" &&
    GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-s -w -X main.version=$V" -o "$OUT_ABS/${BINARIES[0]}" . &&
    GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w -X main.version=$V" -o "$OUT_ABS/${BINARIES[1]}" .)
  rm -rf "$work"
  same=1
  for n in "${BINARIES[@]}"; do
    a=$(sha256sum "$PUB/$n" | cut -d' ' -f1)
    b=$(sha256sum "$OUT_ABS/$n" | cut -d' ' -f1)
    if [ "$a" = "$b" ]; then echo "$ref: identical $n $a"; else echo "$ref: DIFFERENT $n published=$a rebuilt=$b"; same=0; fi
  done
  if [ "$same" = 1 ]; then echo "REBUILT FROM $ref"; exit 0; fi
done
rm -f "$OUT_ABS"/skirnir-agent-*
echo "no ref reproduces the published binaries"
exit 1
