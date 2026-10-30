#!/usr/bin/env python3
"""Keep docs/adr/README.md in step with the ADR files (ADR-0001).

Every `docs/adr/NNNN-*.md` needs front matter with `status` and `date` and a `# Title`; the numbers are consecutive from
0001 without gaps; `superseded by ADR-NNNN` must point at an existing ADR. The index table in `docs/adr/README.md` is
generated from the files: `--write` rewrites it, the default only checks that it matches (CI).

Run: python test/check_adr_index.py           (check; exit 1 on a finding)
     python test/check_adr_index.py --write   (regenerate the index table)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs" / "adr"
INDEX = ADR_DIR / "README.md"
STATUSES = ("proposed", "accepted", "rejected", "deprecated")
BEGIN, END = "<!-- adr-index:begin -->", "<!-- adr-index:end -->"


def read_adr(path):
    """(number, title, status, date) of one ADR, or a list of findings."""
    text = path.read_text(encoding="utf-8")
    findings = []
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    meta = dict(re.findall(r"^(\w[\w-]*):\s*(.+)$", fm.group(1), re.M)) if fm else {}
    if not fm:
        findings.append(f"{path.name}: no front matter")
    status, date = meta.get("status", "").strip(), meta.get("date", "").strip()
    if not (status in STATUSES or re.fullmatch(r"superseded by ADR-\d{4}", status)):
        findings.append(f"{path.name}: status {status!r} - one of {', '.join(STATUSES)} or 'superseded by ADR-NNNN'")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        findings.append(f"{path.name}: date {date!r} - YYYY-MM-DD expected")
    title = re.search(r"^# (.+)$", text, re.M)
    if not title:
        findings.append(f"{path.name}: no '# Title' line")
    return (int(path.name[:4]), title.group(1).strip() if title else "", status, date), findings


def table(rows):
    lines = ["| ADR | Title | Status | Date |", "|---|---|---|---|"]
    for (num, title, status, date), path in rows:
        lines.append(f"| [{num:04d}]({path.name}) | {title} | {status} | {date} |")
    return "\n".join(lines)


def main():
    files = sorted(p for p in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
    findings, rows = [], []
    for p in files:
        row, f = read_adr(p)
        findings += f
        rows.append((row, p))
    numbers = [r[0][0] for r in rows]
    if numbers != list(range(1, len(numbers) + 1)):
        findings.append(f"numbers not consecutive from 0001: {numbers}")
    for (num, _, status, _), _ in rows:
        m = re.fullmatch(r"superseded by ADR-(\d{4})", status)
        if m and int(m.group(1)) not in numbers:
            findings.append(f"ADR-{num:04d}: superseded by ADR-{m.group(1)}, which does not exist")
    index = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
    if BEGIN not in index or END not in index:
        findings.append(f"{INDEX.relative_to(ROOT)}: markers {BEGIN} / {END} missing")
    else:
        head, rest = index.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        wanted = f"{head}{BEGIN}\n{table(rows)}\n{END}{tail}"
        if "--write" in sys.argv:
            INDEX.write_text(wanted, encoding="utf-8", newline="\n")   # LF on every platform (.gitattributes eol=lf)
            print(f"{INDEX.relative_to(ROOT)}: {len(rows)} ADRs written")
        elif wanted != index:
            findings.append(f"{INDEX.relative_to(ROOT)} is out of date - run: python test/check_adr_index.py --write")
    for f in findings:
        print("FINDING", f)
    if not findings and "--write" not in sys.argv:
        print(f"ADR index ok: {len(rows)} ADRs")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
