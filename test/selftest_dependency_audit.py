#!/usr/bin/env python3
"""Self-test (TM-16, ADR-0081): the dependency audit in `.github/workflows/security.yml` also runs on a schedule, at
least weekly, and the scheduled run still audits the hash-locked sets and the Go modules.

ADR-0074 relies on a weekly `pip-audit` because Dependabot does not refresh the hash locks; without a schedule a
vulnerability published after the last push surfaces nowhere. Rejected variants first, then the shipped workflow.
Runs without router and network."""
import copy
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"

# (job, text one of its run steps must contain): what the scheduled run has to keep auditing
REQUIRED_RUNS = (
    ("python-security", "pip-audit --require-hashes --disable-pip -r router/requirements-lock.txt"),
    ("python-security", "pip-audit --require-hashes --disable-pip -r requirements-ci-lock.txt"),
    ("python-security", "lock_requirements.py verify-pypi"),
    ("go-security", "govulncheck ./..."),
)


def triggers(workflow):
    """The `on:` mapping. PyYAML reads YAML 1.1, where the bare key `on` is the boolean True."""
    if "on" in workflow:
        return workflow["on"]
    return workflow.get(True)


def at_least_weekly(cron):
    """A five-field cron expression that fires every week: any minute/hour/weekday, every day of month and month."""
    fields = cron.split() if isinstance(cron, str) else []
    return len(fields) == 5 and fields[2] == "*" and fields[3] == "*"


def findings(workflow):
    """Everything that keeps the audit from running unattended at least weekly, as a list of messages."""
    if not isinstance(workflow, dict):
        return ["workflow is not a mapping"]
    found = []
    on = triggers(workflow)
    if not isinstance(on, dict):
        return ["workflow has no trigger mapping"]
    for event in ("push", "pull_request"):
        if event not in on:
            found.append(f"trigger {event} is missing")
    schedule = on.get("schedule")
    crons = [entry.get("cron") for entry in schedule if isinstance(entry, dict)] if isinstance(schedule, list) else []
    if not crons:
        found.append("no schedule trigger: a vulnerability published after the last push is never reported")
    elif not any(at_least_weekly(cron) for cron in crons):
        found.append(f"schedule {crons} runs less often than weekly")
    jobs = workflow.get("jobs") if isinstance(workflow.get("jobs"), dict) else {}
    for name, needle in REQUIRED_RUNS:
        job = jobs.get(name)
        if not isinstance(job, dict):
            found.append(f"job {name} is missing")
            continue
        if "if" in job:
            found.append(f"job {name} has a condition; the scheduled run must include it unconditionally")
        steps = [step for step in job.get("steps") or [] if isinstance(step, dict)]
        matching = [step for step in steps if needle in str(step.get("run", ""))]
        if not matching:
            found.append(f"job {name} no longer runs: {needle}")
        elif all("if" in step for step in matching):
            found.append(f"job {name} runs '{needle}' only under a condition")
    return list(dict.fromkeys(found))


failures = []


def expect(label, result, count):
    ok = len(result) == count
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  {result}"))
    if not ok:
        failures.append(label)


shipped = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def variant(change):
    workflow = copy.deepcopy(shipped)
    change(workflow)
    return findings(workflow)


def drop_schedule(workflow):
    triggers(workflow).pop("schedule", None)


def monthly(workflow):
    triggers(workflow)["schedule"] = [{"cron": "29 4 1 * *"}]


def daily(workflow):
    triggers(workflow)["schedule"] = [{"cron": "29 4 * * *"}]


def skip_on_schedule(workflow):
    workflow["jobs"]["go-security"]["if"] = "github.event_name != 'schedule'"


def drop_router_lock_audit(workflow):
    for step in workflow["jobs"]["python-security"]["steps"]:
        if "run" in step:
            step["run"] = step["run"].replace("-r router/requirements-lock.txt", "-r router/requirements.txt")


def drop_govulncheck(workflow):
    steps = workflow["jobs"]["go-security"]["steps"]
    workflow["jobs"]["go-security"]["steps"] = [s for s in steps if "govulncheck ./..." not in str(s.get("run", ""))]


def lock_audit_only_on_push(workflow):
    for step in workflow["jobs"]["python-security"]["steps"]:
        if "requirements-ci-lock.txt" in str(step.get("run", "")) and "pip-audit" in str(step.get("run", "")):
            step["if"] = "github.event_name == 'push'"


def drop_pull_request(workflow):
    del triggers(workflow)["pull_request"]


expect("workflow without schedule is rejected", variant(drop_schedule), 1)
expect("monthly schedule is rejected", variant(monthly), 1)
expect("job skipped on the scheduled run is rejected", variant(skip_on_schedule), 1)
expect("missing audit of the router lock is rejected", variant(drop_router_lock_audit), 1)
expect("missing govulncheck is rejected", variant(drop_govulncheck), 1)
expect("lock audits only on push are rejected (one step, both locks)", variant(lock_audit_only_on_push), 2)
expect("dropping the pull request trigger is rejected", variant(drop_pull_request), 1)
expect("a non-mapping is rejected", findings(["on", "push"]), 1)
expect("daily schedule is accepted", variant(daily), 0)
expect("shipped security.yml runs weekly and audits locks and Go modules", findings(shipped), 0)

if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("dependency audit schedule: all checks passed")
