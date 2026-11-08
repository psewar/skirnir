#!/usr/bin/env python3
"""Regression tests for the security boundaries added in the November 2026 review."""
import asyncio
import hashlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import auth, cloud, config, registry, state  # noqa: E402

def check(ok, label):
    if not ok:
        raise AssertionError(label)
    print("PASS", label)

class Request(dict):
    def __init__(self, path, *, remote="192.0.2.11", method="POST", headers=None):
        super().__init__()
        self.path, self.remote, self.method = path, remote, method
        self.headers = headers or {}

async def main():
    state.CFG = SimpleNamespace(
        client_auth={"mode": "enforce", "clients": {}},
        control_users={"operator": auth.hash_password("test-secure-password")},
        control_listen="127.0.0.1:11435",
        cloud={"enabled": True, "data_classes": ["public", "internal", "personal", "secret"],
               "default_data_class": "personal", "max_cloud_data_class": "internal", "credential_scan": "block"})
    state.CLIENT_AUTH_MODE = None
    handler = AsyncMock(return_value="allowed")
    refused = await auth.client_auth_middleware(Request("/api/chat"), handler)
    check(refused.status == 401 and not handler.await_count, "empty enforce client list denies inference")
    state.CFG.client_auth["mode"] = "observe"
    check(await auth.client_auth_middleware(Request("/api/chat"), handler) == "allowed", "explicit observe remains compatible")
    state.CFG.client_auth["mode"] = "enforce"

    # The tests use one genuine Basic header, but no server or external services.
    import base64
    token = base64.b64encode(b"operator:test-secure-password").decode()
    header = {"Authorization": "Basic " + token}
    denied = await auth.basic_auth_middleware(Request("/admin/config", headers=header), handler)
    check(denied.status == 403, "cross-site form without custom header is rejected")
    permitted = await auth.basic_auth_middleware(Request("/admin/config", headers={**header, "X-Skirnir-UI": "1"}), handler)
    check(permitted == "allowed", "valid UI custom header permits admin action")
    denied = await auth.basic_auth_middleware(Request("/admin/config", headers={**header, "X-Skirnir-UI": "1", "Sec-Fetch-Site": "cross-site"}), handler)
    check(denied.status == 403, "cross-site fetch with forged custom header is rejected")
    state.CFG.control_users = {}
    state.CFG.control_listen = "localhost:11435"
    denied = await auth.basic_auth_middleware(Request("/admin/state"), handler)
    check(denied.status == 503, "network-facing control without users is denied")

    req = SimpleNamespace(execution="auto", data_class="public", cloud_privacy=False, data_class_effective=None)
    state.CFG.client_auth["clients"] = {"member": {"data_class": "personal"}}
    refused = cloud.request_block_reason(req, {"messages": []}, "member")
    check(refused is not None and req.data_class_effective == "personal", "caller cannot downgrade personal -> public")
    req2 = SimpleNamespace(execution="auto", data_class="secret", cloud_privacy=False, data_class_effective=None)
    cloud.request_block_reason(req2, {"messages": []}, "member")
    check(req2.data_class_effective == "secret", "caller may upgrade classification")
    # ADR-0043: without a configured class the client declares its own (ADR-0014); undeclared stays at the default
    req3 = SimpleNamespace(execution="auto", data_class="internal", cloud_privacy=False, data_class_effective=None)
    open_ = cloud.request_block_reason(req3, {"messages": []}, "unconfigured")
    check(req3.data_class_effective == "internal" and open_ is None, "unconfigured client may declare internal")
    req4 = SimpleNamespace(execution="auto", data_class=None, cloud_privacy=False, data_class_effective=None)
    shut = cloud.request_block_reason(req4, {"messages": []}, "unconfigured")
    check(req4.data_class_effective == "personal" and shut is not None, "undeclared request stays at the default personal")

    # TM-01/V1: an invalid credential must not inherit an allowed source-IP identity.
    state.CFG.client_auth["clients"] = {"limited": {"ip": ["192.0.2.11"],
        "token_sha256": hashlib.sha256(b"synthetic-good-token").hexdigest(),
        "roles": ["allowed"], "models": False, "cloud": False}}
    handler.reset_mock()
    denied = await auth.client_auth_middleware(Request("/api/chat", headers={"Authorization": "Bearer synthetic-wrong-token"}), handler)
    check(denied.status == 401 and handler.await_count == 0, "bad token never falls back to allowed IP")
    identified = Request("/api/chat", headers={"Authorization": "Bearer synthetic-good-token"})
    check(await auth.client_auth_middleware(identified, handler) == "allowed", "valid token reaches inference authorization")
    role = {"name": "allowed", "exposed": "allowed:latest"}
    check(auth.client_allows(identified, role, concrete=True) is not None, "models:false denies concrete names")
    check(auth.client_allows(identified, role, concrete=False) is None, "permitted role remains available")
    check(auth.client_allows(identified, {"name": "forbidden", "exposed": "forbidden:latest"}, concrete=False) is not None,
          "ungranted role is denied")
    for execution in ("auto", "cloud"):
        optout = SimpleNamespace(execution=execution, data_class="public", cloud_privacy=False, data_class_effective=None)
        check(cloud.request_block_reason(optout, {"messages": []}, "limited") is not None,
              "cloud:false blocks even public content and explicit cloud requests")

    # TM-04 / ADR-0052: concrete model names need an explicit grant; a missing setting no longer means true.
    concrete_role = {"name": "qwen-synthetic:7b", "exposed": "qwen-synthetic:7b"}
    state.CFG.client_auth["clients"] = {"role-only": {"roles": ["*"]}, "granted": {"roles": ["*"], "models": True},
                                        "truthy": {"roles": ["*"], "models": "yes"}}
    for name, allowed, label in (("role-only", False, "missing models setting denies concrete names"),
                                 ("granted", True, "models:true keeps concrete names"),
                                 ("truthy", False, "only the boolean true grants concrete names"),
                                 (state.INTERNAL_CLIENT, True, "the router's own UI client keeps concrete names")):
        req = Request("/api/chat")
        req["client"] = name
        check((auth.client_allows(req, concrete_role, concrete=True) is None) is allowed, label)
    parsed = SimpleNamespace()
    check(config.Config._parse_client_auth(parsed, {"mode": "enforce", "clients": {"role-only": {"roles": ["*"]}}}) is None
          and parsed.client_auth["clients"]["role-only"]["models"] is False, "config: missing models becomes false")
    rejected = config.Config._parse_client_auth(SimpleNamespace(), {"mode": "enforce", "clients": {"bad": {"models": "yes"}}})
    check(rejected is not None, "config: a non-boolean models value is rejected")

    registry._ENROLL.clear()
    state.REG = SimpleNamespace(nodes={})
    for i in range(registry.MAX_ENROLL_PER_IP):
        check(registry.enrollment_denial(str(i), "192.0.2.19", now=100) is None, "new agent within rate cap")
    check(registry.enrollment_denial("overflow", "192.0.2.19", now=100) is not None, "agent rate cap")
    state.REG.nodes["approved"] = {"state": "approved"}
    check(registry.enrollment_denial("approved", "192.0.2.19", now=100) is None, "approved agent can reconnect")
    state.REG.nodes.update({f"pending-{i}": {"state": "pending"} for i in range(registry.MAX_PENDING_NODES)})
    check(registry.enrollment_denial("new-key", "198.51.100.20", now=400) is not None, "pending capacity bounds even a new source IP")
    check(registry.enrollment_denial("approved", "198.51.100.20", now=400) is None, "full pending registry does not block approved reconnect")

    # ADR-0044: the service runs without capabilities; files it cannot read are reported before loading.
    readable, missing = os.path.abspath(__file__), os.path.join(HERE, "no-such-key.pem")
    blocked = config.unreadable_files(SimpleNamespace(control_tls=(readable, missing)))
    check(blocked == [("control_tls.key", missing)], "unreadable TLS key is reported, readable cert is not")
    check(config.unreadable_files(SimpleNamespace(control_tls=None)) == [], "no TLS configured, nothing to read")
    unit = open(os.path.join(HERE, "..", "router", "skirnir-router.service"), encoding="utf-8").read().splitlines()
    check("CapabilityBoundingSet=" not in unit or "ProtectHome=true" not in unit,
          "unit without capabilities does not use ProtectHome=true (EACCES on /root breaks the session store)")

asyncio.run(main())
