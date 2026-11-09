"""Agent signing keys in the ops folder (ADR-0058, ADR-0066), separate from deploy.py so it can be tested without SSH.

    python deploy.py --create-agent-key                   first key of a new installation; refuses if one exists
    python deploy.py --new-agent-key                      rotation step 1: next key, print its public part for update.public_keys
    python deploy.py --use-next-agent-key                 rotation step 2: sign with it, keep the old key as retired
    python deploy.py --replace-lost-agent-key "<reason>"  the signing key is lost: new key, reason on record, node steps printed

deploy.py --agent never creates a key: a missing key is either a new installation or a lost key, and only the operator
knows which (before ADR-0066 a lost key silently became a new one, and every node refused the next release).
"""
import base64
import os
import time


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
    return "missing", (f"No signing key {current}. New installation: deploy.py --create-agent-key. Lost key: "
                       'deploy.py --replace-lost-agent-key "<reason>" (ADR-0066). --agent does not create one.')


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
