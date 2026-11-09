"""Software bills of materials for a release (ADR-0075, TM-16), CycloneDX 1.6 JSON.

    python tools/security/sbom.py agent  --out-dir DIR skirnir-agent-<version>-<os>-<arch>[.exe] ...
    python tools/security/sbom.py router --version <router version> --out FILE

agent: one SBOM per binary from its embedded Go build information (`go version -m -json`, so `go` must be on PATH): the
binary's SHA-256, Go toolchain, build settings and every module with version and go.sum hash. router: the router's
Python requirements - exact, hashed entries from router/requirements-lock.txt, and the requirements a host's
distribution provides, with their version ranges. Output is deterministic for the same input (serial number derived
from the content, no timestamp), so a rebuild produces the same file.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "router", "skirnir_router"))
import pydeps  # noqa: E402

AGENT_NAME = re.compile(r"^skirnir-agent-(?P<version>\d+\.\d+\.\d+)-(?P<os>[a-z0-9]+)-(?P<arch>[a-z0-9]+)(\.exe)?$")
REPO = "github.com/psewar/skirnir"
TOOL = {"type": "application", "name": "tools/security/sbom.py", "group": REPO}


def bom(component, components, properties=()):
    body = {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
            "metadata": {"tools": {"components": [TOOL]}, "component": component,
                         "properties": [{"name": k, "value": v} for k, v in properties]},
            "components": components}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, REPO + '/sbom/' + digest)}",
            **{k: v for k, v in body.items() if k not in ("bomFormat", "specVersion")}}


def agent_bom(info, filename, sha256):
    """CycloneDX document for one agent binary from its `go version -m -json` data."""
    m = AGENT_NAME.match(filename)
    if m is None:
        return None
    version = m.group("version")
    settings = {s["Key"]: s["Value"] for s in info.get("Settings") or []}
    component = {"type": "application", "bom-ref": f"skirnir-agent@{version}", "name": "skirnir-agent", "version": version,
                 "purl": f"pkg:golang/{REPO}/agent@v{version}?goos={m.group('os')}&goarch={m.group('arch')}",
                 "hashes": [{"alg": "SHA-256", "content": sha256}],
                 "properties": [{"name": "file", "value": filename}]}
    go = info.get("GoVersion", "")
    components = [{"type": "platform", "bom-ref": f"go@{go}", "name": "go", "version": go.removeprefix("go"),
                   "purl": f"pkg:golang/stdlib@{go.removeprefix('go')}", "description": "Go toolchain and standard library"}]
    for d in info.get("Deps") or []:
        dep = d.get("Replace") or d
        c = {"type": "library", "bom-ref": f"{dep['Path']}@{dep['Version']}", "name": dep["Path"], "version": dep["Version"],
             "purl": f"pkg:golang/{dep['Path']}@{dep['Version']}"}
        if dep.get("Sum"):
            c["properties"] = [{"name": "go:sum", "value": dep["Sum"]}]
        if d.get("Replace"):
            c.setdefault("properties", []).append({"name": "go:replaces", "value": f"{d['Path']}@{d['Version']}"})
        components.append(c)
    props = [(f"go:build:{k}", settings[k]) for k in sorted(settings)]
    return bom(component, components, props)


def router_bom(version):
    lock, bad = pydeps.read_lock(open(os.path.join(ROOT, "router", "requirements-lock.txt"), encoding="utf-8").read())
    if bad:
        return None
    reqs = pydeps.read_requirements(os.path.join(ROOT, "router", "requirements-session.txt"))
    components, seen = [], set()
    for key in sorted(lock):
        e = lock[key]
        seen.add(key)
        components.append({"type": "library", "bom-ref": f"{key}@{e['version']}", "name": e["name"], "version": e["version"],
                           "purl": f"pkg:pypi/{key}@{e['version']}",
                           "hashes": [{"alg": "SHA-256", "content": h} for h in e["hashes"]],
                           "properties": [{"name": "skirnir:source", "value": "requirements-lock.txt (pip, hash-checked)"}]})
    for r in reqs:
        key = pydeps.canonicalize_name(r.name)
        if key in seen:
            continue
        seen.add(key)
        components.append({"type": "library", "bom-ref": f"{key}{r.specifier}", "name": r.name, "version": str(r.specifier),
                           "purl": f"pkg:pypi/{key}",
                           "properties": [{"name": "skirnir:source", "value": "distribution package (e.g. apt), version range"}]
                           + ([{"name": "skirnir:marker", "value": str(r.marker)}] if r.marker else [])})
    component = {"type": "application", "bom-ref": f"skirnir-router@{version}", "name": "skirnir-router", "version": version,
                 "purl": f"pkg:github/psewar/skirnir@router-{version}"}
    return bom(component, components)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("agent")
    s.add_argument("--out-dir", required=True)
    s.add_argument("binaries", nargs="+")
    s = sub.add_parser("router")
    s.add_argument("--version", required=True)
    s.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if a.command == "router":
        doc = router_bom(a.version)
        if doc is None:
            print("router/requirements-lock.txt has malformed lines - run lock_requirements.py check", file=sys.stderr)
            return 1
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=1, sort_keys=False)
        print(f"{a.out}: {len(doc['components'])} components")
        return 0
    os.makedirs(a.out_dir, exist_ok=True)
    for path in a.binaries:
        name = os.path.basename(path)
        if AGENT_NAME.match(name) is None:
            print(f"{name}: not named skirnir-agent-<version>-<os>-<arch>", file=sys.stderr)
            return 1
        run = subprocess.run(["go", "version", "-m", "-json", path], capture_output=True, text=True)
        if run.returncode != 0:
            print(f"{name}: go version -m failed: {run.stderr.strip()[:300]}", file=sys.stderr)
            return 1
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        doc = agent_bom(json.loads(run.stdout), name, sha)
        out = os.path.join(a.out_dir, re.sub(r"\.exe$", "", name) + ".cdx.json")
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=1)
        print(f"{out}: {len(doc['components'])} components, sha256 {sha[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
