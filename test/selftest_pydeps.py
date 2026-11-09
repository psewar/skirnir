"""Self-test (ADR-0074, TM-16): router/skirnir_router/pydeps.py plans only hash-locked packages the system lacks.

Synthetic requirements, lock and installed sets - no pip, no network:
  * met by the distribution -> nothing planned; unmet and locked -> the lock line with its hashes;
  * pip-installed in the venv with another version than the lock -> planned again; same version -> nothing;
  * unmet and not locked, a locked version outside the specifier, an entry without hash, a malformed line -> refused;
  * markers (win32-only, extras) are honoured; -r includes are followed; verify names every unmet requirement;
  * the shipped router lock covers what the router needs on top of Debian 12's packages, and every entry has hashes.
    python test/selftest_pydeps.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "router", "skirnir_router"))
import pydeps  # noqa: E402

FAILS = []
H1, H2 = "a" * 64, "b" * 64
LINUX = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "python_version": "3.11"}


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def reqs_from(text, extra=None):
    d = tempfile.mkdtemp()
    if extra:
        for name, body in extra.items():
            open(os.path.join(d, name), "w", encoding="utf-8").write(body)
    p = os.path.join(d, "req.txt")
    open(p, "w", encoding="utf-8").write(text)
    return pydeps.read_requirements(p)


LOCK = (f"# header\nasyncpg==0.32.0 \\\n    --hash=sha256:{H1} \\\n    --hash=sha256:{H2}\n"
        f"uvloop==0.23.0 --hash=sha256:{H1}\npaho-mqtt==2.1.0 \\\n    --hash=sha256:{H2}\n")
lock, bad = pydeps.read_lock(LOCK)
check(not bad and set(lock) == {"asyncpg", "uvloop", "paho-mqtt"} and lock["asyncpg"]["hashes"] == [H1, H2],
      "lock: continuations joined, names canonical, hashes kept")

reqs = reqs_from("-r base.txt\nasyncpg>=0.29,<1\n", {"base.txt": "aiohttp>=3.8.4\nPyYAML>=6.0  # comment\n"
                                                      "paho-mqtt>=1.6.1\nuvloop>=0.17; sys_platform == 'never'\n"})
check([r.name for r in reqs] == ["aiohttp", "PyYAML", "paho-mqtt", "uvloop", "asyncpg"], "requirements: -r include and comments")

debian12 = {"aiohttp": ("3.8.4", False), "pyyaml": ("6.0", False), "paho-mqtt": ("1.6.1", False), "asyncpg": ("0.27.0", False)}
lines, problems = pydeps.plan(reqs, lock, debian12)
check(not problems and len(lines) == 1 and lines[0].startswith("asyncpg==0.32.0 ") and f"--hash=sha256:{H2}" in lines[0],
      f"Debian 12: only asyncpg from the lock, with every hash ({lines}, {problems})")

venv_same = dict(debian12, asyncpg=("0.32.0", True))
lines, problems = pydeps.plan(reqs, lock, venv_same)
check(not lines and not problems, "venv already holds the locked version: nothing to do")

venv_other = dict(debian12, asyncpg=("0.30.0", True))
lines, problems = pydeps.plan(reqs, lock, venv_other)
check(len(lines) == 1 and lines[0].startswith("asyncpg==0.32.0"), "venv holds another version than the lock: replaced")

system_other = dict(debian12, asyncpg=("0.30.0", False))
lines, problems = pydeps.plan(reqs, lock, system_other)
check(not lines and not problems, "a distribution package that meets the requirement stays")

no_paho = {k: v for k, v in debian12.items() if k != "paho-mqtt"}
lines, problems = pydeps.plan(reqs, lock, no_paho)
check(sorted(x.split("==")[0] for x in lines) == ["asyncpg", "paho-mqtt"], "missing paho-mqtt comes from the lock")

lines, problems = pydeps.plan(reqs, {k: v for k, v in lock.items() if k != "asyncpg"}, debian12)
check(problems and "not in the lock" in problems[0], "unmet and not locked: refused")

tight = reqs_from("asyncpg>=0.33\n")
lines, problems = pydeps.plan(tight, lock, debian12)
check(problems and "does not meet" in problems[0], "locked version outside the specifier: refused")

nohash, _ = pydeps.read_lock("asyncpg==0.32.0\n")
lines, problems = pydeps.plan(reqs, nohash, debian12)
check(problems and "without --hash" in problems[0], "lock entry without hash: refused")

_, bad = pydeps.read_lock("asyncpg>=0.32 --hash=sha256:" + H1 + "\n")
check(bad, "a range in the lock is a malformed line")

weird = dict(venv_same, aiohttp=("not a version", False))
check(pydeps.verify(reqs, weird) == ["aiohttp>=3.8.4: not a version"], "verify: an unreadable installed version counts as unmet")
check(pydeps.verify(reqs, venv_same) == [], "verify: all met")

shipped, bad = pydeps.read_lock(open(os.path.join(ROOT, "router", "requirements-lock.txt"), encoding="utf-8").read())
check(not bad and shipped and all(e["hashes"] for e in shipped.values()), "router/requirements-lock.txt: every entry hashed")
router_reqs = pydeps.read_requirements(os.path.join(ROOT, "router", "requirements-session.txt"))
lines, problems = pydeps.plan(router_reqs, shipped, env=LINUX, have={"aiohttp": ("3.8.4", False), "pyyaml": ("6.0", False),
                                                     "cryptography": ("38.0.4", False), "paho-mqtt": ("1.6.1", False),
                                                     "uvloop": ("0.17.0", False)})
check(not problems and [x.split("==")[0] for x in lines] == ["asyncpg"],
      f"shipped lock on top of Debian 12 packages: only asyncpg is installed ({problems})")
lines, problems = pydeps.plan(router_reqs, shipped, env=LINUX, have={"aiohttp": ("3.8.4", False), "pyyaml": ("6.0", False),
                                                     "cryptography": ("38.0.4", False)})
check(not problems and sorted(x.split("==")[0] for x in lines) == ["asyncpg", "paho-mqtt", "uvloop"],
      "shipped lock without paho-mqtt and uvloop from apt: all three come from the lock")

if FAILS:
    print(f"{len(FAILS)} failure(s)")
    sys.exit(1)
print("selftest_pydeps: all passed")
