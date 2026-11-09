"""Agent signing keys and signed agent releases (ADR-0058, ADR-0066, ADR-0068). Standalone: needs only Python and the
`cryptography` package, no SSH and no deploy environment. The key folder is --keys <dir> (or $SKIRNIR_OPS); keep it
off the router.

    python agent_keys.py --keys <dir> state                       is there a signing key? (public part)
    python agent_keys.py --keys <dir> create                      first key of a new installation; refuses if one exists
    python agent_keys.py --keys <dir> next                        rotation step 1: next key, public part for update.public_keys
    python agent_keys.py --keys <dir> use-next                    rotation step 2: sign with it, keep the old key as retired
    python agent_keys.py --keys <dir> replace-lost "<reason>"     the signing key is lost: new key, reason on record
    python agent_keys.py --keys <dir> sign-release <dist> --version <v> [--out <dir>]
                                                                  sign agent binaries: manifest.json + renamed files in <out>
                                                                  (default <dist>/publish), to copy to the router's
                                                                  /etc/skirnir-router/agent/

The same steps exist as deploy.py options (--create-agent-key, --new-agent-key, --use-next-agent-key,
--replace-lost-agent-key, --agent) for the maintainer's deploy. deploy.py --agent never creates a key.

deploy.py --agent never creates a key: a missing key is either a new installation or a lost key, and only the operator
knows which (before ADR-0066 a lost key silently became a new one, and every node refused the next release).
"""
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time

# (file in a dist folder, os, arch); a release asset may also be named skirnir-agent-<version>-<os>-<arch>[.exe]
AGENT_BINARIES = (("skirnir-agent.exe", "windows", "amd64"), ("skirnir-agent-linux-amd64", "linux", "amd64"))
VERSION_RX = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def _public_of(seed_b64):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


KEY, NEXT = "agent-update.key", "agent-update.next.key"


def _write_new_key(path):
    """Create an Ed25519 seed at path, owner-only from the start (O_EXCL, 0600); returns its public key (base64)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    seed = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                      serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(base64.b64encode(seed).decode())
    return _public_of(base64.b64encode(seed).decode())


def signing_key_state(ops):
    """("present", public key) or ("missing", what to do) - for deploy.py --agent, checked before any connection."""
    current = os.path.join(ops, KEY)
    if os.path.exists(current):
        return "present", _public_of(open(current, encoding="utf-8").read().strip())
    return "missing", (f"No signing key {current}. New installation: agent_keys.py create (or deploy.py --create-agent-key). "
                       'Lost key: agent_keys.py replace-lost "<reason>" (or deploy.py --replace-lost-agent-key; ADR-0066). '
                       "Signing never creates a key.")


def create_agent_key(ops):
    """First key of a new installation. Refuses if a signing or prepared key exists. Returns the exit code."""
    current, nxt = os.path.join(ops, KEY), os.path.join(ops, NEXT)
    for path in (current, nxt):
        if os.path.exists(path):
            print(f"{path} exists: this is not a new installation (rotation: --new-agent-key; lost key: --replace-lost-agent-key).")
            return 2
    pub = _write_new_key(current)
    print(f"Signing key: {current}")
    print(f"Put this public key into update.public_key on every node: {pub}")
    return 0


def replace_lost_agent_key(ops, reason):
    """The signing key is lost (deleted, unreadable, leaked and destroyed). Refuses without a reason or while the key
    still exists. Writes the new key and agent-update.recovery-<time>.txt (reason and public key, no secret) and prints
    the node steps: until every node trusts the new key, it refuses new releases and keeps its binary."""
    current, nxt = os.path.join(ops, KEY), os.path.join(ops, NEXT)
    if not isinstance(reason, str) or len(reason.strip()) < 8:
        print('A reason is required (at least 8 characters): --replace-lost-agent-key "<what happened>".')
        return 2
    if os.path.exists(current):
        print(f"{current} still exists: it is not lost. Rotate with --new-agent-key / --use-next-agent-key instead.")
        return 2
    if os.path.exists(nxt):
        print(f"A prepared key {nxt} exists (public {_public_of(open(nxt, encoding='utf-8').read().strip())}); if the nodes "
              "already trust it, --use-next-agent-key makes it the signing key without a recovery. Otherwise remove it deliberately.")
        return 2
    pub = _write_new_key(current)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    record = os.path.join(ops, f"agent-update.recovery-{stamp}.txt")
    with open(record, "w", encoding="utf-8") as f:
        f.write(f"time: {time.strftime('%Y-%m-%dT%H:%M:%S')}\nreason: {reason.strip()}\nnew public key: {pub}\n")
    print(f"New signing key: {current} (record: {record})")
    print("Every node still trusts only the lost key and refuses new releases until its configuration is changed.")
    print(f"On every node set update.public_key to {pub} and remove the lost key from update.public_keys")
    print(f"(Windows, as administrator: agent\\Set-UpdateKey.cmd -Step Replace -Key {pub}).")
    return 0


def load_signing_key(ops):
    """(Ed25519 private key, public base64) of the current signing key; call signing_key_state first."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    seed = base64.b64decode(open(os.path.join(ops, KEY), encoding="utf-8").read().strip())
    key = Ed25519PrivateKey.from_private_bytes(seed)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(pub).decode()


def release_files(dist, version):
    """[(path, os, arch, published name)] found in dist: the release-asset name for this version, else the build name."""
    found = []
    for build_name, os_name, arch in AGENT_BINARIES:
        published = f"skirnir-agent-{version}-{os_name}-{arch}" + (".exe" if os_name == "windows" else "")
        for candidate in (published, build_name):
            path = os.path.join(dist, candidate)
            if os.path.isfile(path):
                found.append((path, os_name, arch, published))
                break
    return found


def signed_manifest(key, version, files):
    """{"manifest": ..., "signature": ...} over the canonical JSON the agent verifies (sorted keys, compact, UTF-8)."""
    manifest = {"version": version, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files}
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {"manifest": manifest, "signature": base64.b64encode(key.sign(canonical)).decode()}


def sign_release(ops, dist, version, out=None):
    """Sign the agent binaries in dist for version: writes manifest.json and the binaries under their release names into
    out (default dist/publish). Refuses without a signing key, with a malformed version, without binaries or when out
    already holds a manifest. Returns the exit code."""
    state, detail = signing_key_state(ops)
    if state != "present":
        print(detail)
        return 2
    if not isinstance(version, str) or not VERSION_RX.fullmatch(version):
        print("--version must look like 0.29.1 (the version the binaries report with `skirnir-agent version`).")
        return 2
    files = release_files(dist, version)
    if not files:
        print(f"No agent binaries in {dist}: expected {', '.join(n for n, _, _ in AGENT_BINARIES)} or their release names.")
        return 2
    out = out or os.path.join(dist, "publish")
    if os.path.exists(os.path.join(out, "manifest.json")):
        print(f"{out} already holds a manifest.json; choose an empty --out folder.")
        return 2
    os.makedirs(out, exist_ok=True)
    entries = []
    for path, os_name, arch, published in files:
        data = open(path, "rb").read()
        entries.append({"name": published, "os": os_name, "arch": arch, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        if os.path.abspath(path) != os.path.abspath(os.path.join(out, published)):
            shutil.copyfile(path, os.path.join(out, published))
    key, pub = load_signing_key(ops)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(signed_manifest(key, version, entries), f, indent=1, ensure_ascii=False)
    print(f"Signed agent {version}: {', '.join(e['name'] for e in entries)} and manifest.json in {out}")
    print("Copy these files to /etc/skirnir-router/agent/ on the router (readable by the router); the web UI then offers")
    print(f"the update. Nodes need this public key in update.public_key or update.public_keys: {pub}")
    return 0


def main(argv=None):
    """Command line; see the module docstring. Exit codes: 0 done, 2 refused (reason printed)."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    keys = os.environ.get("SKIRNIR_OPS")
    if argv[0] == "--keys":
        if len(argv) < 2:
            print("--keys needs a folder")
            return 2
        keys, argv = argv[1], argv[2:]
    if not keys:
        print("Name the key folder: --keys <dir> (or set SKIRNIR_OPS).")
        return 2
    if not os.path.isdir(keys):
        print(f"Key folder {keys} does not exist (create it, readable only by you, off the router).")
        return 2
    command, rest = (argv[0], argv[1:]) if argv else ("", [])
    if command == "state":
        state, detail = signing_key_state(keys)
        print(f"signing key: {state}" + (f", public {detail}" if state == "present" else f" - {detail}"))
        return 0
    if command == "create":
        return create_agent_key(keys)
    if command == "next":
        return new_agent_key(keys)
    if command == "use-next":
        return use_next_agent_key(keys)
    if command == "replace-lost":
        return replace_lost_agent_key(keys, rest[0] if rest else "")
    if command == "sign-release":
        if not rest or rest[0].startswith("--"):
            print("sign-release needs the folder with the built binaries")
            return 2
        version = rest[rest.index("--version") + 1] if "--version" in rest and rest.index("--version") + 1 < len(rest) else None
        out = rest[rest.index("--out") + 1] if "--out" in rest and rest.index("--out") + 1 < len(rest) else None
        return sign_release(keys, rest[0], version, out)
    print(f"unknown command {command!r}; see --help")
    return 2


def new_agent_key(ops):
    """Rotation step 1 (ADR-0058): agent-update.next.key next to the current key. Refuses if one is already prepared or no
    current key exists (then there is nothing to rotate; --agent creates the first key). Returns the exit code."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    current, nxt = os.path.join(ops, "agent-update.key"), os.path.join(ops, "agent-update.next.key")
    if not os.path.exists(current):
        print(f"No current key {current}: nothing to rotate (deploy.py --agent creates the first one).")
        return 2
    if os.path.exists(nxt):
        print(f"{nxt} already exists, public key {_public_of(open(nxt, encoding='utf-8').read().strip())}. "
              "Finish the rotation with --use-next-agent-key or remove the file deliberately.")
        return 2
    seed = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                      serialization.NoEncryption())
    fd = os.open(nxt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(base64.b64encode(seed).decode())
    print(f"Next signing key: {nxt}")
    print(f"Add this public key to update.public_keys on every node (keep the current one): {_public_of(base64.b64encode(seed).decode())}")
    print("Then run --use-next-agent-key; afterwards remove the old public key from the nodes.")
    return 0


def use_next_agent_key(ops):
    """Rotation step 2: the prepared next key becomes the signing key; the old one is kept as agent-update.retired-<date>.key
    so manifests can still be checked or re-signed during the switch. Refuses without a prepared key."""
    current, nxt = os.path.join(ops, "agent-update.key"), os.path.join(ops, "agent-update.next.key")
    if not os.path.exists(nxt):
        print(f"No prepared key {nxt}: run --new-agent-key first and add its public key to every node.")
        return 2
    retired = os.path.join(ops, f"agent-update.retired-{time.strftime('%Y%m%d-%H%M%S')}.key")
    if os.path.exists(current):
        os.replace(current, retired)
    os.replace(nxt, current)
    print(f"Signing key is now the former next key (public {_public_of(open(current, encoding='utf-8').read().strip())}).")
    if os.path.exists(retired):
        print(f"Old key kept as {retired}. Remove its public key from update.public_key/public_keys on the nodes once all "
              "run a release signed with the new key; set modes.agent_update.public_key on the router if you use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
