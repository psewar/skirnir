#!/usr/bin/env python3
"""Review writes held for a shared memory scope (ADR-0145) as one of its reviewers.

    python tools/memory_review.py list --project <alias> [--limit N] [--offset N]
    python tools/memory_review.py show --project <alias> <operation id>
    python tools/memory_review.py approve <operation id>
    python tools/memory_review.py reject <operation id>

The router's API address and the reviewer client's token come from the environment, never from arguments (a process
list shows arguments): SKIRNIR_REVIEW_URL (for example https://router.example:11434) and SKIRNIR_REVIEW_TOKEN.
SKIRNIR_REVIEW_CA may name a CA file for a router certificate the system does not trust. Plain http is refused except to
a loopback address, so the token never travels in clear text over a network.

Exit status: 0 done, 1 the router refused or could not be reached (its answer is printed), 2 a precondition is missing.
"""
import argparse
import ipaddress
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

PREFIX = "/v1/skirnir/memory"
PAGE = 50
SHOW_PAGES = 200   # show looks through at most 10000 held writes, the largest review_max_held
TIMEOUT_S = 30
IPV4 = re.compile(r"\d{1,3}(\.\d{1,3}){3}")


def settings(environ):
    """(base url, token, ssl context), or (None, reason) when a precondition is missing."""
    url, token = environ.get("SKIRNIR_REVIEW_URL", ""), environ.get("SKIRNIR_REVIEW_TOKEN", "")
    if not url or not token:
        return None, "set SKIRNIR_REVIEW_URL and SKIRNIR_REVIEW_TOKEN (the reviewer client's API token)"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment or parts.username:
        return None, "SKIRNIR_REVIEW_URL must be an http(s) address without credentials, query or fragment"
    if parts.scheme == "http" and not loopback(parts.hostname):
        return None, "SKIRNIR_REVIEW_URL uses plain http to a non-loopback host; use https so the token is not sent in clear text"
    ca = environ.get("SKIRNIR_REVIEW_CA")
    if ca and not os.path.isfile(ca):
        return None, f"SKIRNIR_REVIEW_CA does not name a file: {ca}"
    context = ssl.create_default_context(cafile=ca or None) if parts.scheme == "https" else None
    return (url.rstrip("/"), token, context), None


def loopback(host):
    """True for localhost, ::1 and 127.0.0.0/8, checked by pattern before the address is parsed."""
    if host in ("localhost", "::1"):
        return True
    return bool(IPV4.fullmatch(host)) and all(int(p) < 256 for p in host.split(".")) and ipaddress.ip_address(host).is_loopback


def call(conf, path, body):
    """(HTTP status, decoded JSON answer). A network or TLS fault becomes status 0 with its type."""
    base, token, context = conf
    request = urllib.request.Request(base + PREFIX + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=context) as answer:
            return answer.status, decoded(answer.read())
    except urllib.error.HTTPError as refused:   # network boundary: the router answered with an error status
        with refused:
            raw = refused.read()
        return refused.code, decoded(raw)
    except (urllib.error.URLError, OSError) as fault:   # network boundary: not reachable, refused or TLS failed
        return 0, {"error": {"message": f"router not reachable: {type(fault).__name__}"}}


def decoded(raw):
    """The router's JSON object, or an error object holding the start of a non-JSON answer."""
    try:
        value = json.loads(raw)
    except ValueError:   # an untrusted answer is JSON only if it parses
        return {"error": {"message": raw.decode("utf-8", "replace")[:200]}}
    return value if isinstance(value, dict) else {"error": {"message": "unexpected answer"}}


def page(conf, project, limit, offset):
    return call(conf, "/review", {"project": project, "limit": limit, "offset": offset})


def run(args, conf, out):
    if args.command == "list":
        status, answer = page(conf, args.project, args.limit, args.offset)
        if status != 200:
            return refused(status, answer, out)
        for item in answer["items"]:
            text = " ".join(str(item.get("content") or "").split())
            out.write(f"{item['operation_id']}  writer={item['writer']}  expires={item['expires_at']}\n    {text[:160]}\n")
        out.write(f"{len(answer['items'])} held write(s) on this page"
                  + (f", {len(answer['withheld'])} withheld by the content filter" if answer["withheld"] else "") + "\n")
        return 0
    if args.command == "show":
        for number in range(SHOW_PAGES):
            status, answer = page(conf, args.project, PAGE, number * PAGE)
            if status != 200:
                return refused(status, answer, out)
            found = [i for i in answer["items"] if i["operation_id"] == args.operation]
            if found:
                out.write(json.dumps(found[0], indent=1, ensure_ascii=False) + "\n")
                return 0
            if len(answer["items"]) + len(answer["withheld"]) < PAGE:
                break
        out.write(f"operation {args.operation} is not held for review in project {args.project}\n")
        return 1
    status, answer = call(conf, f"/operations/{args.operation}/{args.command}", {})
    if status != 200:
        return refused(status, answer, out)
    out.write(f"{args.operation}: {answer['state']}" + (f" ({answer['reason']})" if answer.get("reason") else "") + "\n")
    return 0


def refused(status, answer, out):
    error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
    detail = error.get("reason") or error.get("message") or answer.get("reason") or ""
    state = f" (state {answer['state']})" if answer.get("state") else ""
    out.write(f"refused: HTTP {status}{state} {detail}\n" if status else f"refused: {detail}\n")
    return 1


def parser():
    p = argparse.ArgumentParser(description="Review writes held for a shared memory scope (ADR-0145).")
    sub = p.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="list held writes, oldest first")
    listing.add_argument("--project", required=True, help="your project alias bound to the reviewed shared scope")
    listing.add_argument("--limit", type=int, default=20, choices=range(1, PAGE + 1), metavar="1..50")
    listing.add_argument("--offset", type=int, default=0)
    show = sub.add_parser("show", help="print one held write in full")
    show.add_argument("--project", required=True)
    show.add_argument("operation")
    for name, text in (("approve", "release a held write for delivery"), ("reject", "refuse a held write; its text is dropped")):
        sub.add_parser(name, help=text).add_argument("operation")
    return p


def main(argv=None, environ=None, out=None):
    args = parser().parse_args(argv)
    out = out or sys.stdout
    if getattr(args, "offset", 0) < 0:
        out.write("--offset must be at least 0\n")
        return 2
    conf, reason = settings(os.environ if environ is None else environ)
    if conf is None:
        out.write(reason + "\n")
        return 2
    return run(args, conf, out)


if __name__ == "__main__":
    sys.exit(main())
