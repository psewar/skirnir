"""Agent signing-key rotation in the ops folder (ADR-0058), separate from deploy.py so it can be tested without SSH.

    python deploy.py --new-agent-key        step 1: next key, print its public part for update.public_keys
    python deploy.py --use-next-agent-key   step 2: sign with it, keep the old key as retired
"""
import base64
import os
import time


def _public_of(seed_b64):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


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
