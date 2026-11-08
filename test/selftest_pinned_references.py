"""Self-test for test/check_pinned_references.py (TM-16 / ADR-0055): rejected and accepted references."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_pinned_references as pins  # noqa: E402

SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
DIGEST = "sha256:" + "a" * 64
failures = []


def expect(label, findings, count):
    ok = len(findings) == count
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  {findings}"))
    if not ok:
        failures.append(label)


wf = pins.workflow_findings
expect("tag-referenced action is rejected", wf("      - uses: actions/checkout@v7\n", "w.yml"), 1)
expect("branch-referenced action is rejected", wf("      - uses: actions/checkout@main\n", "w.yml"), 1)
expect("short SHA is rejected", wf("      - uses: actions/checkout@3d3c42e\n", "w.yml"), 1)
expect("full SHA with version comment is accepted", wf(f"      - uses: actions/checkout@{SHA} # v7.0.1\n", "w.yml"), 0)
expect("job-level reusable workflow at a SHA is accepted", wf(f"    uses: org/repo/.github/workflows/x.yml@{SHA}\n", "w.yml"), 0)
expect("local action is accepted", wf("      - uses: ./.github/actions/local\n", "w.yml"), 0)
expect("go install @latest is rejected", wf("      - run: go install golang.org/x/vuln/cmd/govulncheck@latest\n", "w.yml"), 1)
expect("go install without version is rejected", wf("      - run: go install example.org/tool\n", "w.yml"), 1)
expect("go install at a release is accepted", wf("      - run: go install golang.org/x/vuln/cmd/govulncheck@v1.9.0\n", "w.yml"), 0)
expect("unpinned pip tool is rejected", wf("      - run: python -m pip install -r router/requirements.txt pip-audit\n", "w.yml"), 1)
expect("pinned pip tool and requirement files are accepted",
       wf("      - run: python -m pip install -r router/requirements.txt pip-audit==2.10.1\n", "w.yml"), 0)
expect("commented-out action is ignored", wf("      # - uses: actions/checkout@v7\n", "w.yml"), 0)

cf = pins.compose_findings
expect("registry image by tag is rejected", cf("    image: grafana/grafana:13.3.0\n", "compose.yaml"), 1)
expect("registry image without tag is rejected", cf("    image: grafana/grafana\n", "compose.yaml"), 1)
expect("tag with digest is accepted", cf(f"    image: grafana/grafana:13.3.0@{DIGEST}\n", "compose.yaml"), 0)
expect("locally built image name is accepted", cf("    image: skirnir-embed\n", "compose.yaml"), 0)

df = pins.dockerfile_findings
expect("base image by tag is rejected", df("FROM python:3.11-slim\n", "Dockerfile"), 1)
expect("base image with digest is accepted", df(f"FROM python:3.11-slim@{DIGEST}\n", "Dockerfile"), 0)
expect("scratch and earlier stage are accepted",
       df(f"FROM golang:1.27@{DIGEST} AS build\nFROM build\nFROM scratch\n", "Dockerfile"), 0)

expect("repository has no unpinned references", pins.check(pins.tracked_files(),
       lambda n: (pins.ROOT / n).read_text(encoding="utf-8")), 0)

print(f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
