"""Self-test for test/check_training_data.py (ADR-0077): rejected and accepted cases, without the real data."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_training_data as check  # noqa: E402

failures = []


def expect(label, findings, count):
    ok = len(findings) == count
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  {findings}"))
    if not ok:
        failures.append(label)


sp = check.spelling_findings
expect("ss for ß is rejected", sp("Wie heisst der Berg?", "t"), 1)
expect("ß is accepted", sp("Wie heißt der Berg?", "t"), 0)
expect("two such words on two lines give two findings", sp("draussen\nFussball-WM", "t"), 2)
expect("a word with a real ss is accepted", sp("Aussage, Wasser, muss, Klasse, Schluss", "t"), 0)
expect("an English word is accepted", sp("process the class, pass the message", "t"), 0)
expect("ASCII role name gross is accepted", sp('"options": ["standard", "gross", "assist", "code"]', "t"), 0)
expect("ss for ß inside JSON is rejected", sp('{"text": "Was ist ausser Haus?"}', "t"), 1)

with tempfile.TemporaryDirectory() as committed, tempfile.TemporaryDirectory() as built:
    for d, text in ((committed, "a\n"), (built, "a\n")):
        with open(os.path.join(d, "train.jsonl"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    expect("identical data is accepted", check.compare_findings(committed, built, ["train.jsonl"], "builder.py"), 0)
    with open(os.path.join(built, "train.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write("b\n")
    expect("committed data that the builder no longer produces is rejected",
           check.compare_findings(committed, built, ["train.jsonl"], "builder.py"), 1)
    expect("a file the builder did not write is rejected",
           check.compare_findings(committed, built, ["validation.jsonl"], "builder.py"), 1)
    with open(os.path.join(built, "test.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write("c\n")
    expect("a missing committed file is rejected", check.compare_findings(committed, built, ["test.jsonl"], "builder.py"), 1)

expect("model without data digest is rejected", check.model_findings({"classes": []}, "abc"), 1)
expect("model trained on other data is rejected", check.model_findings({"data_sha256": "abc"}, "def"), 1)
expect("model trained on the committed data is accepted", check.model_findings({"data_sha256": "abc"}, "abc"), 0)

print("ALL OK" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
