"""Restore progress exclusively from a previous default-branch scheduled/manual full review."""
import json
import os
from pathlib import Path
import subprocess


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path]))


def restore(repository, branch, directory, current):
    runs = []
    for event in ("schedule", "workflow_dispatch"):
        response = api(f"repos/{repository}/actions/workflows/threat-review.yml/runs?event={event}&per_page=100")
        runs.extend(run for run in response["workflow_runs"] if run["id"] != current and run["head_branch"] == branch
                    and run["event"] == event and run["status"] == "completed" and run["head_repository"]["full_name"] == repository)
    for run in sorted(runs, key=lambda r: r["id"], reverse=True):
        artifacts = api(f"repos/{repository}/actions/runs/{run['id']}/artifacts")["artifacts"]
        if any(a["name"] == "full-review-coverage" and not a["expired"] for a in artifacts):
            subprocess.run(["gh", "run", "download", str(run["id"]), "--repo", repository, "--name", "full-review-coverage",
                            "--dir", str(directory)], check=True)
            if not (directory / "coverage.json").is_file():
                raise ValueError("coverage artifact lacks its ledger")
            print(f"Restored full-review coverage from run {run['id']}.")
            return
    print("No retained full-review ledger. Starting a new coverage cycle.")


if __name__ == "__main__":
    restore(os.environ["GITHUB_REPOSITORY"], os.environ["DEFAULT_BRANCH"], Path(os.environ["RUNNER_TEMP"]) / "full-coverage",
            int(os.environ["GITHUB_RUN_ID"]))
