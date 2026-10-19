#!/usr/bin/env python3
"""Enforce AGENTS.md "Explicit control flow" in router code.

Every `except` handler and every `raise` statement needs a marker comment
`# exception-ok: <reason>` on the same line or on the comment line directly above.
Inside a handler, error text (`str(exc)`, `repr(exc)`, `exc.args`) must not be
compared or searched; that counts as a finding as well, marker or not.

Files not yet converted have a per-file ratchet in control_flow_baseline.json: the
number of findings may not grow, and when it shrinks the baseline must be lowered
in the same change. New files start at zero.

Run: python test/check_control_flow.py            (check; exit 1 on violations)
     python test/check_control_flow.py --update   (rewrite the baseline to the current counts)
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SOURCES = [os.path.join("router", "router.py")] + sorted(
    os.path.join("router", "skirnir_router", dirpath[len(os.path.join(ROOT, "router", "skirnir_router")) + 1:], name)
    for dirpath, _, files in os.walk(os.path.join(ROOT, "router", "skirnir_router"))
    for name in files if name.endswith(".py"))
BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_flow_baseline.json")
MARKER = re.compile(r"#\s*exception-ok:\s*\S")
TEXT_METHODS = {"startswith", "endswith", "find", "rfind", "index", "rindex", "count", "split", "partition"}


def marked(lines, lineno):
    """Marker on the statement's first line or on a comment-only line directly above it."""
    if MARKER.search(lines[lineno - 1]):
        return True
    above = lineno - 2
    return above >= 0 and lines[above].lstrip().startswith("#") and bool(MARKER.search(lines[above]))


def error_text(node, names):
    """True when node is str(exc)/repr(exc)/exc.args for a name bound by an enclosing handler."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "repr") and node.args:
        return isinstance(node.args[0], ast.Name) and node.args[0].id in names
    return isinstance(node, ast.Attribute) and node.attr == "args" and isinstance(node.value, ast.Name) and node.value.id in names


def derived(node, names):
    return any(error_text(sub, names) for sub in ast.walk(node))


def findings(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return scan(fh.read(), path)


def scan(source, name="<source>"):
    """(line, message) for every finding in Python source text."""
    lines = source.splitlines()
    tree = ast.parse(source, name)
    out = []

    def visit(node, names, reported):
        if isinstance(node, ast.ExceptHandler):
            if not marked(lines, node.lineno):
                out.append((node.lineno, "except without '# exception-ok: <reason>'"))
            names = names | ({node.name} if node.name else set())
        elif isinstance(node, ast.Raise) and not marked(lines, node.lineno):
            out.append((node.lineno, "raise without '# exception-ok: <reason>'"))
        if names and not reported:   # one finding per expression, not one per nested call inside it
            if isinstance(node, ast.Compare) and any(derived(part, names) for part in [node.left, *node.comparators]):
                out.append((node.lineno, "error text compared (give the case a status, field or category)"))
                reported = True
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in TEXT_METHODS
                  and derived(node.func.value, names)):
                out.append((node.lineno, "error text searched (give the case a status, field or category)"))
                reported = True
        for child in ast.iter_child_nodes(node):
            visit(child, names, reported)

    visit(tree, set(), False)
    return out


def main():
    current = {path.replace(os.sep, "/"): findings(path) for path in SOURCES}
    counts = {path: len(items) for path, items in current.items() if items}
    if "--update" in sys.argv:
        with open(BASELINE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(dict(sorted(counts.items())), fh, indent=2)
            fh.write("\n")
        print(f"baseline written: {sum(counts.values())} findings in {len(counts)} files")
        return 0
    with open(BASELINE, encoding="utf-8") as fh:
        baseline = json.load(fh)
    errors = []
    for path, items in sorted(current.items()):
        allowed = baseline.get(path, 0)
        if len(items) > allowed:
            errors.append(f"{path}: {len(items)} findings, baseline allows {allowed}")
            errors.extend(f"  {path}:{line}: {message}" for line, message in items)
        elif len(items) < allowed:
            errors.append(f"{path}: {len(items)} findings, lower its baseline from {allowed} (python test/check_control_flow.py --update)")
    stale = sorted(set(baseline) - set(counts))
    errors.extend(f"{path}: no findings left, remove it from the baseline (--update)" for path in stale if baseline[path])
    if errors:
        print("Explicit control flow (AGENTS.md):")
        print("\n".join(errors))
        return 1
    print(f"control flow ok: {sum(counts.values())} legacy findings in {len(counts)} files (baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
