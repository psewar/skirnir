#!/usr/bin/env python3
"""Check added lines for common publication mistakes; print locations, never matches.

This supplements a human privacy review. It cannot recognize arbitrary personal
names or prove that natural-language text contains no identifying information.
"""
import argparse
import ipaddress
import re
import subprocess
import sys

TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16})\b")
EMAIL = re.compile(r"\b[A-Za-z0-9_.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
PRIVATE_HOST = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9.-]*\.(?:local|lan|home|internal|corp)\b", re.I)
CREDENTIAL_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.I)
USER_PATH = re.compile(r"(?:/home/(?!runner/)[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\[^\\\s]+\\)")
EXAMPLE_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
# Go's local time zone looks like a ".local" host to PRIVATE_HOST; only this exact spelling is exempt.
GO_TIME_LOCAL = re.compile(r"\btime\.Local\b")


def findings(line):
    found = []
    for label, pattern in (("possible credential", TOKEN), ("private hostname", PRIVATE_HOST),
                           ("credential in URL", CREDENTIAL_URL), ("user-specific path", USER_PATH)):
        if pattern.search(GO_TIME_LOCAL.sub("", line) if pattern is PRIVATE_HOST else line):
            found.append(label)
    for match in EMAIL.finditer(line):
        host = match[1].lower()
        if not (host.endswith((".invalid", ".example", ".test")) or host in ("example.com", "example.net", "example.org")):
            found.append("non-example email")
    for match in IPV4.finditer(line):
        try:
            address = ipaddress.ip_address(match[0])
        except ValueError:
            continue
        if not address.is_loopback and not any(address in network for network in EXAMPLE_NETWORKS):
            found.append("non-example IP address")
    return sorted(set(found))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()
    # Resolve first; option-looking strings cannot become git diff options.
    base = subprocess.check_output(["git", "rev-parse", "--verify", args.base + "^{commit}"], text=True).strip()
    patch = subprocess.check_output(["git", "diff", "--no-ext-diff", "--no-color", "--unified=0", base, "--"], text=True)
    failures, path, line_no = [], "", 0
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            line_no = int(re.search(r"\+(\d+)", line)[1]) - 1
        elif line.startswith("+") and not line.startswith("+++"):
            line_no += 1
            categories = findings(line[1:])
            if categories:
                failures.append(f"{path}:{line_no}: {', '.join(categories)}")
        elif line.startswith(" "):
            line_no += 1
    for failure in failures:
        print(failure)
    print(f"Public-change scan: {len(failures)} finding(s); manual privacy review still required")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
