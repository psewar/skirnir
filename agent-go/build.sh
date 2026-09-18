#!/bin/bash
# Baut die Windows-Binary aus WSL. Aufruf: wsl -e bash -lc '/mnt/c/<workspace>/skirnir/agent-go/build.sh'
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.local/go/bin:$PATH"
export GOFLAGS=-mod=mod
VERSION="${1:-$(date +%Y.%m.%d)-$(date +%H%M)}"
mkdir -p dist
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$VERSION" -o dist/ollama-router-agent.exe .
ls -la dist/ollama-router-agent.exe
echo "version $VERSION"
