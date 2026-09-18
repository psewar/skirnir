#!/usr/bin/env python3
"""py-spy-Rohprofil (perf-profile.txt, gestapelte Stacks) in Selbstzeit und Inklusivzeit je Funktion umrechnen.

    python perf_profile_report.py [perf-profile.txt] [--top 25]
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
path = next((a for a in sys.argv[1:] if a.endswith(".txt")), os.path.join(HERE, "perf-profile.txt"))
top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 25
ROUTER = os.path.normpath(os.path.join(HERE, "..", "router")) + os.sep
PYLIB = os.path.normpath(os.path.join(os.path.dirname(sys.executable), "Lib")) + os.sep


def short(frame):
    m = re.match(r"(.*?) \((.*)\)$", frame)
    if not m:
        return frame[:100]
    fn, loc = m.groups()
    loc = loc.replace(ROUTER, "").replace(PYLIB, "lib/")
    return f"{fn} [{loc}]"[:100]


leaf, incl, total = collections.Counter(), collections.Counter(), 0
for line in open(path, encoding="utf-8", errors="replace"):
    line = line.rstrip("\n")
    if " " not in line:
        continue
    stack, cnt = line.rsplit(" ", 1)
    if not cnt.isdigit():
        continue
    cnt = int(cnt)
    frames = stack.split(";")
    total += cnt
    leaf[frames[-1]] += cnt
    for f in set(frames):
        incl[f] += cnt

print("Samples gesamt:", total)
print(f"\n== Selbstzeit (Blatt) Top {top} ==")
for f, c in leaf.most_common(top):
    print(f"{100 * c / total:5.1f}%  {short(f)}")
print(f"\n== Inklusivzeit eigene Module (ollama_router) Top {top} ==")
own = sorted(((f, c) for f, c in incl.items() if "ollama_router" in f), key=lambda x: -x[1])
for f, c in own[:top]:
    print(f"{100 * c / total:5.1f}%  {short(f)}")
