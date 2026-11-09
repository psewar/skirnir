#!/usr/bin/env python3
"""The decision models' training data is what its builders produce, in the standard German spelling, and the tf-idf
router model was trained on exactly the committed data (ADR-0077). Standard library only; CI runs it.

    python test/check_training_data.py
Checks:
1. decision-eval/build_dataset.py and decision-embed/privacy/build_dataset.py, run into a temporary folder, produce the
   committed data files byte for byte. A changed template or slot list without regenerated data fails here.
2. router/decision-tfidf.json records `data_sha256` equal to the digest of decision-eval/data (data_digest.py). Data
   regenerated without retraining the model fails here.
3. German text in the builders, the generated data, the hand-written privacy test set and the model's vocabulary writes
   ß where the standard spelling has it.
Exit 0: consistent. 1: findings, each with the next step. 2: a builder could not run (its output is printed).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL = os.path.join(ROOT, "decision-eval")
PRIVACY = os.path.join(ROOT, "decision-embed", "privacy")
MODEL = os.path.join(ROOT, "router", "decision-tfidf.json")
sys.path.insert(0, EVAL)
from data_digest import digest  # noqa: E402

BUILDERS = (
    (os.path.join(EVAL, "build_dataset.py"), os.path.join(EVAL, "data"),
     ("train.jsonl", "validation.jsonl", "test.jsonl", "summary.json")),
    (os.path.join(PRIVACY, "build_dataset.py"), os.path.join(PRIVACY, "data"), ("train.jsonl", "validation.jsonl")),
)
SPELLING_FILES = [os.path.join(EVAL, "build_dataset.py"), os.path.join(PRIVACY, "build_dataset.py"), MODEL] + [
    os.path.join(d, n) for _, d, names in BUILDERS for n in names if n.endswith(".jsonl")] + [
    os.path.join(PRIVACY, "data", "test.jsonl")]
# German words whose standard spelling has ß, written with ss instead. Only forms that are neither
# an English word nor a standard German one.
SS_FOR_ESZETT = re.compile(
    r"(?i)(?<![\wäöüß])(?:aussen\w*|ausser\w*|äusser\w*|grösse\w*|grösst\w*|heiss\w*|weiss|weisst|\w*schliess\w*|gemäss"
    r"|\w*mässig\w*|massnahme\w*|massgeb\w*|strasse\w*|bloss|draussen|süss\w*|\w*fliess\w*|grüss\w*|gruss|spass"
    r"|hiess\w*|liess\w*|\w*giess\w*|fussball\w*|\w*schiess\w*|stiess\w*)(?![\wäöüß])")


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def rel(path: str) -> str:
    """Path relative to the repository when it is inside it, else as given (a temporary folder in the self-test)."""
    full = os.path.abspath(path)
    return os.path.relpath(full, ROOT) if full.startswith(ROOT + os.sep) else path


def spelling_findings(text: str, name: str) -> list[str]:
    """Words written with ss where the standard spelling has ß, one finding per line and word."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        for m in SS_FOR_ESZETT.finditer(line):
            out.append(f"{name}:{no}: {m.group()!r} is not the standard spelling; write ß")
    return out


def compare_findings(expected_dir: str, actual_dir: str, names, builder: str) -> list[str]:
    """Committed files (expected_dir) that differ from what the builder wrote (actual_dir)."""
    out = []
    for n in names:
        committed, built = os.path.join(expected_dir, n), os.path.join(actual_dir, n)
        if not os.path.isfile(built):
            out.append(f"{builder} did not write {n}")
        elif not os.path.isfile(committed):
            out.append(f"{rel(committed)} is missing; run {rel(builder)} and commit its output")
        elif read_bytes(committed) != read_bytes(built):   # not filecmp: it caches by size and mtime
            out.append(f"{rel(committed)} is not what {rel(builder)} produces; "
                       "regenerate the data, retrain the model trained on it and commit both")
    return out


def model_findings(model: dict, data_sha256: str) -> list[str]:
    """The tf-idf model must name the digest of the data it was trained on, and it must be the committed data's."""
    recorded = model.get("data_sha256")
    if not recorded:
        return ["router/decision-tfidf.json records no data_sha256; retrain it with decision-eval/train_tfidf.py"]
    if recorded != data_sha256:
        return ["router/decision-tfidf.json was trained on other data than decision-eval/data; "
                "retrain it with decision-eval/train_tfidf.py"]
    return []


def main() -> int:
    findings = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (builder, committed, names) in enumerate(BUILDERS):
            out = os.path.join(tmp, str(i))
            run = subprocess.run([sys.executable, builder, "--out", out], cwd=os.path.dirname(builder),
                                 capture_output=True, text=True, encoding="utf-8")
            if run.returncode != 0:
                print(f"check_training_data: {rel(builder)} failed (exit {run.returncode}):\n{run.stdout}{run.stderr}")
                return 2
            findings += compare_findings(committed, out, names, builder)
    if os.path.isfile(MODEL):
        with open(MODEL, encoding="utf-8") as f:
            model = json.load(f)
        findings += model_findings(model, digest(os.path.join(EVAL, "data")))
    else:
        findings.append("router/decision-tfidf.json is missing; train it with decision-eval/train_tfidf.py")
    for path in SPELLING_FILES:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                findings += spelling_findings(f.read(), rel(path))
    for f in findings:
        print(f)
    print(f"Training data: {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
