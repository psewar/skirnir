#!/usr/bin/env python3
"""Regression tests for the security boundaries added in the November 2026 review."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import auth, cloud, registry, state  # noqa: E402

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

    registry._ENROLL.clear()
    state.REG = SimpleNamespace(nodes={})
    for i in range(registry.MAX_ENROLL_PER_IP):
        check(registry.enrollment_denial(str(i), "192.0.2.19", now=100) is None, "new agent within rate cap")
    check(registry.enrollment_denial("overflow", "192.0.2.19", now=100) is not None, "agent rate cap")
    state.REG.nodes["approved"] = {"state": "approved"}
    check(registry.enrollment_denial("approved", "192.0.2.19", now=100) is None, "approved agent can reconnect")

asyncio.run(main())
