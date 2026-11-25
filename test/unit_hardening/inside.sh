#!/bin/bash
# Runs inside the throwaway systemd container of test/unit_hardening_check.sh (the checkout is mounted at /src):
# install the router with install.sh, start it under its unit, exercise TLS, the API, the UI, a configuration write and
# a reload, and fail on any wrong answer or a journal line that looks like a refused system call or file access.
set -u
FAILED=0
expect() {   # expect <what> <wanted> <got>
  if [ "$2" = "$3" ]; then echo "ok    $1 ($3)"; else echo "FAIL  $1: wanted $2, got $3"; FAILED=1; fi
}
cp -r /src/router /tmp/router-src
(cd /tmp/router-src && bash install.sh --without-mqtt > /tmp/install.log 2>&1)
expect "install.sh" "STATE: installed" "$(tail -1 /tmp/install.log)"
mkdir -p /etc/skirnir-router/certs
openssl req -x509 -newkey rsa:2048 -nodes -keyout /etc/skirnir-router/certs/privkey.pem \
  -out /etc/skirnir-router/certs/fullchain.pem -days 2 -subj "/CN=router.example.net" 2>/dev/null
chgrp skirnir-router /etc/skirnir-router/certs/privkey.pem && chmod 0640 /etc/skirnir-router/certs/privkey.pem
/opt/skirnir-router/venv/bin/python /src/test/unit_hardening/make_config.py /opt/skirnir-router /etc/skirnir-router/config.yaml
systemctl enable --now skirnir-router > /dev/null 2>&1
for _ in $(seq 1 30); do curl -sk -o /dev/null https://127.0.0.1:11434/api/version && break; sleep 0.5; done
TOKEN='Authorization: Bearer synthetic-unit-test-token'
USER='admin:synthetic-unit-test-password'
code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }
expect "service" "active" "$(systemctl is-active skirnir-router)"
expect "API version over TLS" "200" "$(code https://127.0.0.1:11434/api/version)"
expect "client model list" "200" "$(code -H "$TOKEN" https://127.0.0.1:11434/api/tags)"
expect "chat without a node" "503" "$(code -H "$TOKEN" -d '{"model":"standard","messages":[{"role":"user","content":"x"}],"stream":false}' https://127.0.0.1:11434/api/chat)"
expect "web UI" "200" "$(code -u "$USER" https://127.0.0.1:11435/)"
expect "admin state" "200" "$(code -u "$USER" https://127.0.0.1:11435/admin/state)"
expect "configuration write" "200" "$(code -u "$USER" -H 'X-Skirnir-UI: 1' -H 'Content-Type: application/json' -X PUT -d '{"settings": {"router.request_timeout_s": 300}}' https://127.0.0.1:11435/admin/config)"
expect "roles.yaml in the state folder" "yes" "$([ -s /var/lib/skirnir-router/roles.yaml ] && echo yes || echo no)"
systemctl reload skirnir-router && sleep 1
expect "after reload" "active 200" "$(systemctl is-active skirnir-router) $(code https://127.0.0.1:11434/api/version)"
problems=$(journalctl -u skirnir-router --no-pager -o cat | grep -i -E 'traceback|permission denied|not permitted|operation not permitted|EPERM|read-only file system' || true)
expect "journal without refused calls or files" "" "$problems"
echo "exposure: $(systemd-analyze security skirnir-router 2>/dev/null | tail -1)"
exit $FAILED
