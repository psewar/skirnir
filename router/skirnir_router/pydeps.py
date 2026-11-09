"""Which locked Python packages the router's venv still needs (ADR-0074, TM-16).

The router's venv sees the distribution's packages (--system-site-packages): aiohttp, PyYAML, cryptography, paho-mqtt and
uvloop come from apt, signed by the distribution. What the distribution lacks or ships too old is installed from
router/requirements-lock.txt only - exact versions with SHA-256 hashes, `pip install --require-hashes --no-deps`. This
module decides which lock entries that is, with the interpreter of the venv, before anything is installed:

    python pydeps.py plan   --requirements requirements-session.txt --lock requirements-lock.txt   > plan.txt
    python pydeps.py verify --requirements requirements-session.txt

plan prints the lock lines to install (possibly none) and exits 0; it exits 2 when an unmet requirement has no lock entry
or its locked version does not meet the requirement. A package that pip put into the venv earlier and that differs from its
locked version is planned again, so the venv holds what the lock names. verify exits 0 when every requirement is met by an
installed distribution, 1 otherwise, and names each one.

Standard library plus `packaging`, or the copy vendored in pip when `packaging` is not installed; runs as a plain file
(no package imports), so install.sh and deploy.py can call it before the router is in place.
"""
import argparse
import importlib.metadata
import importlib.util
import os
import re
import sys

if importlib.util.find_spec("packaging") is not None:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import VERSION_PATTERN, Version
elif importlib.util.find_spec("pip") is not None and importlib.util.find_spec("pip._vendor.packaging") is not None:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name
    from pip._vendor.packaging.version import VERSION_PATTERN, Version
else:
    sys.exit("pydeps: neither `packaging` nor pip's vendored copy is importable - install python3-packaging or pip")

LOCK_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)(?P<rest>.*)$")
VERSION_RE = re.compile(r"^\s*" + VERSION_PATTERN + r"\s*$", re.VERBOSE | re.IGNORECASE)
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})\b")


def read_requirements(path, seen=None):
    """Requirement objects of a requirements file, following -r includes relative to the file; markers kept."""
    seen = seen if seen is not None else set()
    path = os.path.abspath(path)
    if path in seen:
        return []
    seen.add(path)
    out = []
    for raw in open(path, encoding="utf-8").read().splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")):
            out += read_requirements(os.path.join(os.path.dirname(path), line.split(None, 1)[1].strip()), seen)
            continue
        if line.startswith("-"):
            continue
        out.append(Requirement(line))
    return out


def parse_version(text):
    """Version, or None for a string packaging cannot read (then the requirement counts as not met)."""
    return Version(text) if text and VERSION_RE.match(text) else None


def meets(req, version):
    v = parse_version(version)
    return v is not None and req.specifier.contains(v, prereleases=True)


def read_lock(text):
    """({canonical name: {"name", "version", "hashes", "line"}}, [malformed lines]) from a hash-pinned lock (backslash
    continuations joined)."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    out, bad = {}, []
    for raw in joined.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        m = LOCK_LINE.match(line)
        if m is None:
            bad.append(line[:80])
            continue
        name = canonicalize_name(m.group("name"))
        hashes = HASH.findall(m.group("rest"))
        out[name] = {"name": m.group("name"), "version": m.group("version"), "hashes": hashes,
                     "line": f"{m.group('name')}=={m.group('version')} " + " ".join(f"--hash=sha256:{h}" for h in hashes)}
    return out, bad


def installed():
    """{canonical name: (version, in_venv)} of every distribution this interpreter sees."""
    venv = sys.prefix != sys.base_prefix
    prefix = os.path.realpath(sys.prefix) + os.sep
    out = {}
    for d in importlib.metadata.distributions():
        name = d.metadata["Name"]
        if not name:
            continue
        key = canonicalize_name(name)
        if key in out:
            continue   # first one on sys.path wins, as for imports
        where = os.path.realpath(str(d.locate_file("")))
        out[key] = (d.version, venv and (where + os.sep).startswith(prefix))
    return out


def active(reqs, env=None):
    """Requirements whose marker holds for this interpreter (or for env, a marker environment overriding it)."""
    e = dict(env or {}, extra="")
    return [r for r in reqs if r.marker is None or r.marker.evaluate(e)]


def plan(reqs, lock, have, env=None):
    """(lines to install, problems)."""
    lines, problems = [], []
    for r in active(reqs, env):
        key = canonicalize_name(r.name)
        got = have.get(key)
        entry = lock.get(key)
        met = got is not None and meets(r, got[0])
        if entry is not None and not meets(r, entry["version"]):
            problems.append(f"{r.name}: locked {entry['version']} does not meet {r}")
            continue
        if entry is not None and not entry["hashes"]:
            problems.append(f"{r.name}: lock entry without --hash")
            continue
        if met and not (got[1] and entry is not None and got[0] != entry["version"]):
            continue
        if entry is None:
            problems.append(f"{r.name}: {r} is not met by the system ({got[0] if got else 'missing'}) and not in the lock")
            continue
        lines.append(entry["line"])
    return lines, problems


def verify(reqs, have, env=None):
    missing = []
    for r in active(reqs, env):
        got = have.get(canonicalize_name(r.name))
        if got is None or not meets(r, got[0]):
            missing.append(f"{r}: {got[0] if got else 'missing'}")
    return missing


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=("plan", "verify"))
    p.add_argument("--requirements", required=True)
    p.add_argument("--lock")
    a = p.parse_args(argv)
    reqs = read_requirements(a.requirements)
    have = installed()
    if a.command == "verify":
        missing = verify(reqs, have)
        for m in missing:
            print(f"not met: {m}", file=sys.stderr)
        print(f"pydeps verify: {len(active(reqs)) - len(missing)} of {len(active(reqs))} requirements met", file=sys.stderr)
        return 1 if missing else 0
    if not a.lock:
        p.error("plan needs --lock")
    lock, bad = read_lock(open(a.lock, encoding="utf-8").read())
    lines, problems = plan(reqs, lock, have)
    problems = [f"lock line is not name==version --hash=sha256:...: {b}" for b in bad] + problems
    for pr in problems:
        print(f"pydeps plan: {pr}", file=sys.stderr)
    if problems:
        return 2
    for line in lines:
        print(line)
    print(f"pydeps plan: {len(lines)} package(s) from the lock", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
