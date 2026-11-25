"""Write the test configuration from config.minimal.yaml: a synthetic control user and client token, the example public
URL, no MQTT. Runs inside the throwaway container of test/unit_hardening_check.sh only.
Usage: make_config.py <installed router dir> <out config.yaml>"""
import hashlib
import os
import sys

import yaml

sys.path.insert(0, sys.argv[1])
from skirnir_router import auth  # noqa: E402

with open(os.path.join(sys.argv[1], "config.minimal.yaml"), encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
r = cfg["router"]
r["public_url"] = "https://router.example.net:11435"
r["control_auth"]["users"] = {"admin": auth.hash_password("synthetic-unit-test-password", iterations=1000)}
clients = r["client_auth"]["clients"]
clients[next(iter(clients))]["token_sha256"] = hashlib.sha256(b"synthetic-unit-test-token").hexdigest()
r.pop("mqtt", None)
with open(sys.argv[2], "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
