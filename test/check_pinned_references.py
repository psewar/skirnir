"""TM-16 / ADR-0055: third-party code that CI or a deployment fetches must be referenced immutably.

Checks, for the files tracked in this repository:
  * workflow `uses:` of another repository: owner/repo@<40-hex commit SHA> (local `./` actions are fine);
  * workflow `go install <module>@<version>`: a release version, never latest/master/main;
  * workflow `pip install <tool>` (not `-r`): pinned with `==`;
  * compose `image:` from a registry: tag@sha256 digest (a bare local image name built here is fine);
  * Dockerfile `FROM`: image@sha256 digest (`scratch` and earlier build stages are fine).

    python test/check_pinned_references.py           check the repository, exit 1 on findings
    python test/selftest_pinned_references.py        rejected and accepted examples
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
GO_VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")


def workflow_findings(text, name):
    found = []
    for no, line in enumerate(text.splitlines(), 1):
        code = line.split(" #", 1)[0].strip()
        m = re.match(r"(?:- )?uses: *(\S+)$", code)
        if m and not m.group(1).startswith("./"):
            ref = m.group(1)
            target, _, version = ref.partition("@")
            if "/" not in target or not SHA.fullmatch(version):
                found.append(f"{name}:{no}: action {ref} is not pinned to a full commit SHA")
        for module in re.findall(r"go install +(\S+)", code):
            version = module.rpartition("@")[2] if "@" in module else ""
            if not GO_VERSION.fullmatch(version):
                found.append(f"{name}:{no}: go install {module} needs a release version (vX.Y.Z)")
        m = re.search(r"pip install +(.+)$", code)
        if m:
            words, skip = m.group(1).split(), False
            for word in words:
                if skip:
                    skip = False
                    continue
                if word in ("-r", "--requirement", "-c", "--constraint"):
                    skip = True
                    continue
                if word.startswith("-") or word.startswith("$") or word.endswith(".txt"):
                    continue
                if "==" not in word:
                    found.append(f"{name}:{no}: pip install {word} needs an exact version (==)")
    return found


def compose_findings(text, name):
    found = []
    for no, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*image:\s*['\"]?([^'\"\s#]+)", line)
        if not m:
            continue
        image = m.group(1)
        local = "/" not in image and ":" not in image and "@" not in image
        if not local and not DIGEST.search(image):
            found.append(f"{name}:{no}: image {image} has no @sha256 digest")
    return found


def dockerfile_findings(text, name):
    found, stages = [], set()
    for no, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, re.I)
        if not m:
            continue
        image = m.group(1)
        if m.group(2):
            stages.add(m.group(2).lower())
        if image.lower() == "scratch" or image.lower() in stages:
            continue
        if not DIGEST.search(image):
            found.append(f"{name}:{no}: base image {image} has no @sha256 digest")
    return found


def tracked_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def check(files, read):
    found = []
    for name in files:
        base = name.rsplit("/", 1)[-1]
        if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", name):
            found += workflow_findings(read(name), name)
        elif re.fullmatch(r"(?:docker-)?compose(?:\.[\w-]+)?\.ya?ml", base):
            found += compose_findings(read(name), name)
        elif base == "Dockerfile" or base.startswith("Dockerfile."):
            found += dockerfile_findings(read(name), name)
    return found


def main():
    findings = check(tracked_files(), lambda name: (ROOT / name).read_text(encoding="utf-8"))
    for f in findings:
        print(f)
    print(f"Pinned references: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
