#!/usr/bin/env python3
"""Self-test (ADR-0127): every image Dependabot updates is built in CI, without a container engine or network.

Dependabot updates the base images of the directories in .github/dependabot.yml (ecosystem docker, ADR-0055). The
router image is built by the "container" job in ci.yml (test/smoke_container.sh); every other directory must be a
service of .github/workflows/decision-images.yml and of test/smoke_decision_images.sh, which builds the image, compiles
its program files and runs its start imports. The workflow runs on pull requests that change a file going into an image
(each Dockerfile and every file its COPY lines take from the build context) and weekly. Before, nothing built the
decision images, and a base whose Python onnxruntime did not support yet looked green (#150, #173).
    python test/selftest_service_images.py
"""
import copy
import os
import re
import shlex
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = "test/smoke_decision_images.sh"
WORKFLOW = ".github/workflows/decision-images.yml"


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def docker_directories(dependabot):
    """Directories (without slashes) whose Dockerfile base images Dependabot updates."""
    dirs = []
    for update in dependabot.get("updates") or []:
        if update.get("package-ecosystem") != "docker":
            continue
        dirs += update.get("directories") or []
        if update.get("directory"):
            dirs.append(update["directory"])
    return sorted(d.strip("/") for d in dirs)


def copied_files(dockerfile):
    """The build-context files a Dockerfile's COPY instructions take (flags such as --chmod left out)."""
    files = []
    for line in dockerfile.replace("\\\n", " ").splitlines():
        words = shlex.split(line, comments=True)
        if not words or words[0].upper() != "COPY":
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        files += args[:-1]
    return files


def script_services(script):
    """SERVICES_ALL of the script: the services it builds by default and accepts by name."""
    m = re.search(r'^SERVICES_ALL="([^"]*)"', script, re.M)
    return m.group(1).split() if m else []


def workflow_triggers(workflow):
    # YAML 1.1 reads the key "on" as the boolean true.
    return workflow.get("on", workflow.get(True)) or {}


def problems(dependabot, workflow, script, dockerfiles, ci):
    """Everything that keeps an image Dependabot updates from being built in CI; empty when all are covered.
    dockerfiles maps each service directory to the text of its Dockerfile."""
    out = []
    docker = set(docker_directories(dependabot))
    if "router" in docker and "test/smoke_container.sh" not in ci:
        out.append("router: Dependabot updates its base image, but ci.yml does not run test/smoke_container.sh")
    services = docker - {"router"}
    job = (workflow.get("jobs") or {}).get("image") or {}
    matrix = set(((job.get("strategy") or {}).get("matrix") or {}).get("service") or [])
    for s in sorted(services - matrix):
        out.append(f"{s}: Dependabot updates its base image, but {WORKFLOW} does not build it")
    for s in sorted(matrix - services):
        out.append(f"{s}: {WORKFLOW} builds it, but Dependabot does not update it (remove it or add it there)")
    for s in sorted(matrix - set(script_services(script))):
        out.append(f"{s}: missing from SERVICES_ALL in {SCRIPT}")
    triggers = workflow_triggers(workflow)
    paths = set(((triggers.get("pull_request") or {}).get("paths")) or [])
    needed = {SCRIPT, WORKFLOW}
    for s in sorted(matrix):
        text = dockerfiles.get(s)
        if text is None:
            out.append(f"{s}: no Dockerfile")
            continue
        needed.add(f"{s}/Dockerfile")
        needed |= {f"{s}/{f}" for f in copied_files(text)}
    for p in sorted(needed - paths):
        out.append(f"{p}: changes it does not trigger {WORKFLOW} (add it to pull_request.paths)")
    if not triggers.get("schedule"):
        out.append(f"{WORKFLOW}: no schedule; the images install the newest packages, a weekly build must catch a new release")
    runs = " ".join(step.get("run", "") for step in job.get("steps") or [])
    if SCRIPT not in runs or '"$SERVICE"' not in runs:
        out.append(f"{WORKFLOW}: the image job does not run {SCRIPT} for its matrix service")
    if workflow.get("permissions") != {"contents": "read"}:
        out.append(f"{WORKFLOW}: permissions must be contents: read only")
    return out


def repository():
    dependabot = yaml.safe_load(read(".github/dependabot.yml"))
    workflow = yaml.safe_load(read(WORKFLOW))
    dockerfiles = {}
    for s in docker_directories(dependabot):
        path = os.path.join(ROOT, s, "Dockerfile")
        if os.path.isfile(path):
            dockerfiles[s] = read(f"{s}/Dockerfile")
    return dependabot, workflow, read(SCRIPT), dockerfiles, read(".github/workflows/ci.yml")


class RepositoryTests(unittest.TestCase):
    def test_every_dependabot_image_is_built_in_ci(self):
        self.assertEqual(problems(*repository()), [])

    def test_the_services_are_the_decision_services(self):
        dependabot, workflow, script, _, _ = repository()
        self.assertEqual(docker_directories(dependabot), ["decision-embed", "decision-jevlike", "router"])
        self.assertEqual(script_services(script), ["decision-embed", "decision-jevlike"])

    def test_copied_files_are_the_program_files(self):
        _, _, _, dockerfiles, _ = repository()
        self.assertEqual(copied_files(dockerfiles["decision-embed"]), ["server.py", "embedder.py"])
        self.assertEqual(copied_files(dockerfiles["decision-jevlike"]), ["server.py"])

    def test_script_and_workflow_check_their_inputs(self):
        script = read(SCRIPT)
        self.assertIn('refuse "unknown service', script)
        self.assertIn("--network none", script)
        self.assertIn("STATE: passed", script)
        # The import check is derived from the program files, not a list per service that could drift.
        self.assertIn("ast.walk(ast.parse(", script)
        self.assertIn('pathlib.Path("/app")', script)
        workflow = yaml.safe_load(read(WORKFLOW))
        checkout = workflow["jobs"]["image"]["steps"][0]
        self.assertRegex(checkout["uses"], r"^actions/checkout@[0-9a-f]{40}$")
        self.assertIs(checkout["with"]["persist-credentials"], False)


class RejectedCases(unittest.TestCase):
    def setUp(self):
        self.base = repository()

    def run_with(self, change):
        dependabot, workflow, script, dockerfiles, ci = copy.deepcopy(self.base)
        args = {"dependabot": dependabot, "workflow": workflow, "script": script, "dockerfiles": dockerfiles, "ci": ci}
        change(args)
        return problems(**args)

    def test_a_new_dependabot_directory_without_a_build(self):
        def change(a):
            docker = next(u for u in a["dependabot"]["updates"] if u["package-ecosystem"] == "docker")
            docker["directories"].append("/decision-new")
        found = self.run_with(change)
        self.assertTrue(any(p.startswith("decision-new: Dependabot updates") for p in found), found)

    def test_a_copied_file_outside_the_trigger_paths(self):
        def change(a):
            a["dockerfiles"]["decision-embed"] += "\nCOPY extra.py /app/\n"
        found = self.run_with(change)
        self.assertIn("decision-embed/extra.py: changes it does not trigger " + WORKFLOW +
                      " (add it to pull_request.paths)", found)

    def test_a_service_missing_from_the_script(self):
        def change(a):
            a["script"] = a["script"].replace('SERVICES_ALL="decision-embed decision-jevlike"',
                                              'SERVICES_ALL="decision-embed"')
        found = self.run_with(change)
        self.assertIn(f"decision-jevlike: missing from SERVICES_ALL in {SCRIPT}", found)

    def test_a_workflow_without_schedule(self):
        def change(a):
            workflow_triggers(a["workflow"]).pop("schedule")
        found = self.run_with(change)
        self.assertTrue(any("no schedule" in p for p in found), found)

    def test_wider_permissions(self):
        def change(a):
            a["workflow"]["permissions"] = {"contents": "write"}
        self.assertIn(f"{WORKFLOW}: permissions must be contents: read only", self.run_with(change))

    def test_the_router_image_without_its_ci_job(self):
        def change(a):
            a["ci"] = a["ci"].replace("test/smoke_container.sh", "test/other.sh")
        found = self.run_with(change)
        self.assertTrue(any(p.startswith("router:") for p in found), found)

    def test_copy_flags_and_continuations(self):
        self.assertEqual(copied_files("COPY --chmod=0755 a.py \\\n     b.py /app/\nRUN true\n"), ["a.py", "b.py"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
