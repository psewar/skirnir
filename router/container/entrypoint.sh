#!/bin/sh
# Entry point of the router image (ADR-0116). Passes every argument to router.py, so the image answers the same commands
# as a host install:
#   (no argument)                         run with /etc/skirnir-router/config.yaml (the image's CMD)
#   --init /etc/skirnir-router [...]      write a first configuration into the mounted folder (docs/install-router.md)
#   --check /etc/skirnir-router/config.yaml [--files]   --posture ...   --hash
# umask 077 like UMask=0077 in skirnir-router.service: files the router creates are owner-only (ADR-0056).
umask 077
exec python /opt/skirnir-router/router.py "$@"
