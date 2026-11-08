"""Self-test (TM-13, ADR-0058): agent signing-key rotation in the ops folder (deploy/agent_keys.py).

Step 1 refuses without a current key and when a next key is already prepared; step 2 refuses without a prepared key,
makes the next key the signing key and keeps the old one as retired. Runs in a temporary folder, no SSH."""
import base64
import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "deploy"))
import agent_keys  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{info}]"))
    if not cond:
        FAILS.append(name)


def run(fn, ops):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = fn(ops)
    return rc, out.getvalue()


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


ops = tempfile.mkdtemp(prefix="skirnir-keys-")
try:
    current, nxt = os.path.join(ops, "agent-update.key"), os.path.join(ops, "agent-update.next.key")
    rc, _ = run(agent_keys.new_agent_key, ops)
    check("step 1 refuses without a current key", rc == 2 and not os.path.exists(nxt))
    rc, _ = run(agent_keys.use_next_agent_key, ops)
    check("step 2 refuses without a prepared key", rc == 2)

    seed = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                      serialization.NoEncryption())
    with open(current, "w", encoding="utf-8") as f:
        f.write(base64.b64encode(seed).decode())
    old_pub = agent_keys._public_of(read(current))

    rc, out = run(agent_keys.new_agent_key, ops)
    check("step 1 prepares the next key", rc == 0 and os.path.exists(nxt), out)
    next_pub = agent_keys._public_of(read(nxt))
    check("step 1 prints the next public key for update.public_keys", next_pub in out and next_pub != old_pub, out)
    check("the current key is untouched by step 1", agent_keys._public_of(read(current)) == old_pub)
    if hasattr(os, "fchmod"):
        check("the next key is owner-only (0600)", stat.S_IMODE(os.stat(nxt).st_mode) == 0o600, oct(os.stat(nxt).st_mode))
    rc, out = run(agent_keys.new_agent_key, ops)
    check("step 1 refuses a second prepared key and names the pending one", rc == 2 and next_pub in out
          and agent_keys._public_of(read(nxt)) == next_pub, out)

    rc, out = run(agent_keys.use_next_agent_key, ops)
    retired = [n for n in os.listdir(ops) if n.startswith("agent-update.retired-")]
    check("step 2 makes the next key the signing key", rc == 0 and agent_keys._public_of(read(current)) == next_pub, out)
    check("step 2 keeps the old key as retired", len(retired) == 1
          and agent_keys._public_of(read(os.path.join(ops, retired[0]))) == old_pub, retired)
    check("no prepared key is left", not os.path.exists(nxt))
    rc, _ = run(agent_keys.use_next_agent_key, ops)
    check("step 2 cannot run twice", rc == 2 and agent_keys._public_of(read(current)) == next_pub)
finally:
    shutil.rmtree(ops, ignore_errors=True)
print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
