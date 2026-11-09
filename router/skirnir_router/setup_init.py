"""`router.py --init <dir>`: a first configuration that is safe and valid (ADR-0067).

Writes <dir>/config.yaml from config.minimal.yaml next to router.py, with
  - a control user whose password is asked without echo (twice on a terminal; one line from stdin otherwise) and stored
    only as a PBKDF2 hash,
  - one client with a freshly generated token, shown once and stored only as sha256,
  - the public URL and the TLS file paths, if given.
Checks before writing: the file does not exist yet, the template is present and has every marker, the password has at
least 12 characters (and matches its repetition), names are plain. The file is created owner-only (0600) and exclusively.
Afterwards the configuration is loaded like --check and the posture report is printed. Last line: INIT: written |
INIT: refused (with the reason above it).
"""
import getpass
import hashlib
import os
import re
import secrets
import sys

from . import auth, config, posture
from .outcome import failed

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.minimal.yaml")
EXAMPLE_HASH = hashlib.sha256(b"changeme").hexdigest()
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
MARKERS = {
    "public_url": "  public_url: https://router.example.net:11435   # REPLACE: the name your nodes and browser use to reach this router\n",
    "cert": "    cert: /etc/skirnir-router/tls/fullchain.pem\n",
    "key": "    key: /etc/skirnir-router/tls/privkey.pem\n",
    "users": '    users: {}                      # REPLACE: {admin: "<hash from: python3 router.py --hash>"}; without users the port answers 503\n',
    "client": "      my-client:                   # one entry per client application; give each its own token\n",
    "token": f'        token_sha256: "{EXAMPLE_HASH}"   # REPLACE: sha256 of the token ("changeme" here)\n',
}


def refuse(message):
    print(message)
    print("INIT: refused")
    return 2


def read_password(stream=None):
    """The password, or None after a mismatch. A terminal is asked twice without echo; a pipe gives one line."""
    stream = sys.stdin if stream is None else stream
    if stream.isatty():
        first = getpass.getpass("Password for the web UI and admin API: ")
        return first if getpass.getpass("Repeat it: ") == first else None
    return stream.readline().rstrip("\r\n")


def init(target, *, public_url=None, user="admin", client="my-client", cert=None, key=None, password=None):
    path = os.path.join(target, "config.yaml")
    if os.path.exists(path):
        return refuse(f"{path} exists; --init writes only a new configuration (edit the existing one, or move it away).")
    if not os.path.isfile(TEMPLATE):
        return refuse(f"template missing: {TEMPLATE} (it ships next to router.py)")
    for label, value in (("user", user), ("client", client)):
        if not NAME.fullmatch(value):
            return refuse(f"--{label} must be 1-64 characters: letters, digits, '.', '_' or '-'.")
    if public_url is not None and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?", public_url):
        return refuse("--public-url must look like https://router.example.net:11435")
    text = open(TEMPLATE, encoding="utf-8").read().replace("\r\n", "\n")
    missing = [k for k, marker in MARKERS.items() if text.count(marker) != 1]
    if missing:
        return refuse(f"template {TEMPLATE} does not have the expected lines: {', '.join(missing)}")
    password = read_password() if password is None else password
    if password is None:
        return refuse("The two passwords differ.")
    if len(password) < 12:
        return refuse("The password needs at least 12 characters.")

    token = secrets.token_urlsafe(32)
    replacements = {
        "users": f'    users: {{{user}: "{auth.hash_password(password)}"}}   # router.py --init; change with router.py --hash\n',
        "client": f"      {client}:                   # router.py --init; one entry per client application, each with its own token\n",
        "token": f'        token_sha256: "{hashlib.sha256(token.encode()).hexdigest()}"   # sha256 of the token router.py --init showed once\n',
    }
    if public_url is not None:
        replacements["public_url"] = f"  public_url: {public_url.rstrip('/')}   # the name nodes and browsers use for this router\n"
    if cert:
        replacements["cert"] = f"    cert: {cert}\n"
    if key:
        replacements["key"] = f"    key: {key}\n"
    for k, new in replacements.items():
        text = text.replace(MARKERS[k], new)
    os.makedirs(target, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)

    cfg = config.Config.load(path, use_overrides=False)
    if failed(cfg):   # the template is part of the release; a failure here is a defect, reported as a fact
        print(f"written, but the configuration does not load: {cfg.message}")
        print("INIT: refused")
        return 2
    print(f"Configuration written: {path} (owner-only)")
    print(f"Control user: {user} (password stored as a hash only)")
    print(f"Client {client} token, shown only now - store it in the client and your password manager:")
    print(f"  {token}")
    lines, summary = posture.report(cfg)
    for line in lines:
        print(line)
    print(summary)
    print("Next: put the TLS certificate and key at the paths in control_tls, pull the model on a GPU node "
          "(ollama pull qwen3:8b), start the router, install an agent, approve it in the web UI.")
    print("INIT: written")
    return 0
