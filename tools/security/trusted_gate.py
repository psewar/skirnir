"""Resolve an open default-branch PR independently of the candidate workflow and verify its Git objects."""
import json
import os
import re
import subprocess
import sys


def open_candidates(pulls, repository, branch, head):
    return [pr for pr in pulls if pr["state"] == "open" and pr["base"]["repo"]["full_name"] == repository
            and pr["base"]["ref"] == branch and pr["head"]["sha"] == head]


def main():
    head = os.environ["REVIEW_HEAD"]
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return 1
    repository, branch = os.environ["GITHUB_REPOSITORY"], os.environ["DEFAULT_BRANCH"]
    # A workflow_run payload can omit PRs for forks. Resolve the open PRs on GitHub, including multiple PRs at one SHA.
    pages = subprocess.check_output(["gh", "api", "--paginate", "--slurp", "-X", "GET", f"repos/{repository}/pulls",
                                     "-f", "state=open", "-f", f"base={branch}", "-F", "per_page=100"])
    pulls = open_candidates([p for page in json.loads(pages) for p in page], repository, branch, head)
    if not pulls:
        print("No current open default-branch PR at the completed run's SHA.")
        return 0
    number, base = pulls[0]["number"], pulls[0]["base"]["sha"]
    if type(number) is not int or not re.fullmatch(r"[0-9a-f]{40}", base):
        return 1
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("eligible=true\n")
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "fetch", "--no-tags", "origin", f"refs/pull/{number}/head", base], check=True)
    # A concurrent force-push must never label its replacement using this run's result.
    if subprocess.check_output(["git", "rev-parse", "FETCH_HEAD"], text=True).strip() != head:
        print("Pull request changed during fetch. Review the new head.")
        return 1
    cmd = [sys.executable, "-I", "tools/security/review.py", "check", "--base", base, "--head", head]
    if all(pr["user"]["login"] == "dependabot[bot]" for pr in pulls):
        cmd.append("--dependency-update")
    # Keep candidate-controlled strings out of Actions workflow commands/logs. The normal PR job gives local diagnostics.
    status = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    print("Independent evidence check passed." if status == 0 else "Independent evidence check failed. Run the base checker locally.")
    return status


if __name__ == "__main__":
    sys.exit(main())
