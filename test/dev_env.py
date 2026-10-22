#!/usr/bin/env python3
"""Development environment for the UI: fake node, fake cloud, fake agent, and router as in run_tests, but without TLS and without
Basic Auth on the control port, so http://127.0.0.1:21435/ works directly in the browser. Runs until Ctrl-C.
Uses a derived configuration dev-config.yaml (from test-config.yaml)."""
import os
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

cfg = yaml.safe_load(open(os.path.join(HERE, "test-config.yaml"), encoding="utf-8"))
cfg["router"].pop("control_tls", None)
cfg["router"].pop("control_auth", None)
cfg["router"]["api_tls"] = False
cfg["router"]["public_url"] = "http://127.0.0.1:21435"
cfg["router"]["client_auth"]["mode"] = "observe"
open(os.path.join(HERE, "dev-config.yaml"), "w", encoding="utf-8").write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
open(os.path.join(HERE, "secrets.env"), "w", encoding="utf-8").write("CLOUD_TEST_KEY=cloud-test-key\nCLAUDE_TEST_KEY=claude-test-key\n")
for f in ("roles.yaml", "nodes.json", "usage.json"):   # usage.json: otherwise the cloud budget from the test run is already exhausted
    try:
        os.remove(os.path.join(HERE, f))
    except FileNotFoundError:
        pass

procs = []
logs = {}
for port, name, models in ((21001, "big", "qwen3.6:35b-a3b,qwen3-coder:30b,granite4.2:8b,local-assist:latest,gpt-oss:20b"), (21002, "small", "granite4.2:8b")):
    logs[name] = open(os.path.join(HERE, f"dev-{name}.log"), "w")
    extra = ["--tls", "test-cert.pem", "test-key.pem", "--require-token", "testtoken"] if name == "big" else []
    procs.append(subprocess.Popen([PY, os.path.join(HERE, "fake_ollama.py"), str(port), name, models, "--delay", "0.3", *extra], cwd=HERE, stdout=logs[name], stderr=subprocess.STDOUT))
logs["cloud"] = open(os.path.join(HERE, "dev-cloud.log"), "w")
procs.append(subprocess.Popen([PY, os.path.join(HERE, "fake_cloud.py"), "21010", "cloud-test-key", "claude-test-key"], cwd=HERE, stdout=logs["cloud"], stderr=subprocess.STDOUT))
logs["router"] = open(os.path.join(HERE, "dev-router.log"), "w")
procs.append(subprocess.Popen([PY, os.path.join(HERE, "..", "router", "router.py"), os.path.join(HERE, "dev-config.yaml")], cwd=HERE, stdout=logs["router"], stderr=subprocess.STDOUT))
time.sleep(2)
logs["agent"] = open(os.path.join(HERE, "dev-agent.log"), "w")
procs.append(subprocess.Popen([PY, os.path.join(HERE, "fake_agent.py"), "ws://127.0.0.1:21435", "big", "https://127.0.0.1:21001", os.path.join(HERE, "fake-agent-big.key"), "testtoken"],
                              cwd=HERE, stdout=logs["agent"], stderr=subprocess.STDOUT))
print("dev environment running: UI http://127.0.0.1:21435/  (agent 'big' waits for approval in the UI; Ctrl-C stops)", flush=True)
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
