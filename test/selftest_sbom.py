"""Self-test (ADR-0075, TM-16): tools/security/sbom.py builds valid, deterministic CycloneDX documents.

Agent: from a synthetic `go version -m -json` record - binary hash, Go toolchain, every module with purl and go.sum, a
replaced module recorded as such, build settings; a wrongly named file is refused. Router: every lock entry with all its
hashes, distribution-provided requirements with their ranges. Same input, same bytes. No Go, no network.
    python test/selftest_sbom.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "security"))
import sbom  # noqa: E402

FAILS = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


INFO = {"GoVersion": "go1.27.3", "Path": "github.com/psewar/skirnir/agent",
        "Main": {"Path": "github.com/psewar/skirnir/agent", "Version": "(devel)"},
        "Deps": [{"Path": "github.com/gorilla/websocket", "Version": "v1.5.3", "Sum": "h1:abc="},
                 {"Path": "gopkg.in/yaml.v3", "Version": "v3.0.1", "Sum": "h1:def=",
                  "Replace": {"Path": "example.org/fork/yaml", "Version": "v3.0.2", "Sum": "h1:ghi="}}],
        "Settings": [{"Key": "CGO_ENABLED", "Value": "0"}, {"Key": "-trimpath", "Value": "true"}]}
SHA = "1" * 64

doc = sbom.agent_bom(INFO, "skirnir-agent-0.31.0-windows-amd64.exe", SHA)
check(doc["bomFormat"] == "CycloneDX" and doc["specVersion"] == "1.6"
      and re.match(r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", doc["serialNumber"]),
      "agent: CycloneDX 1.6 header with an RFC 4122 serial number")
comp = doc["metadata"]["component"]
check(comp["version"] == "0.31.0" and comp["hashes"] == [{"alg": "SHA-256", "content": SHA}]
      and comp["purl"] == "pkg:golang/github.com/psewar/skirnir/agent@v0.31.0?goos=windows&goarch=amd64",
      "agent: component with version, hash and purl")
names = {c["name"]: c for c in doc["components"]}
check(names["go"]["version"] == "1.27.3", "agent: Go toolchain listed")
check(names["github.com/gorilla/websocket"]["purl"] == "pkg:golang/github.com/gorilla/websocket@v1.5.3"
      and {"name": "go:sum", "value": "h1:abc="} in names["github.com/gorilla/websocket"]["properties"],
      "agent: module with purl and go.sum")
rep = names.get("example.org/fork/yaml")
check(rep is not None and {"name": "go:replaces", "value": "gopkg.in/yaml.v3@v3.0.1"} in rep["properties"],
      "agent: replaced module recorded with what it replaces")
check({"name": "go:build:CGO_ENABLED", "value": "0"} in doc["metadata"]["properties"], "agent: build settings kept")
check(json.dumps(doc) == json.dumps(sbom.agent_bom(INFO, "skirnir-agent-0.31.0-windows-amd64.exe", SHA)),
      "agent: deterministic")
check(sbom.agent_bom(INFO, "agent.exe", SHA) is None, "agent: a file not named skirnir-agent-<v>-<os>-<arch> is refused")
other = sbom.agent_bom(INFO, "skirnir-agent-0.31.0-linux-amd64", "2" * 64)
check(other["serialNumber"] != doc["serialNumber"], "agent: another binary gets another serial number")

rdoc = sbom.router_bom("0.24.0")
rnames = {c["name"].lower(): c for c in rdoc["components"]}
lock_entries = [c for c in rdoc["components"] if c["purl"].count("@")]
check(rnames.get("asyncpg") and len(rnames["asyncpg"]["hashes"]) > 1 and rnames["asyncpg"]["purl"].startswith("pkg:pypi/asyncpg@"),
      "router: locked package with all its hashes")
check(rnames.get("aiohttp") and rnames["aiohttp"]["version"].startswith(">=")
      and any(p["value"].startswith("distribution") for p in rnames["aiohttp"]["properties"]),
      "router: distribution-provided requirement with its range")
check(len(lock_entries) >= 3 and rdoc["metadata"]["component"]["version"] == "0.24.0", "router: lock entries and version")
check(json.dumps(rdoc) == json.dumps(sbom.router_bom("0.24.0")), "router: deterministic")

if FAILS:
    print(f"{len(FAILS)} failure(s)")
    sys.exit(1)
print("selftest_sbom: all passed")
