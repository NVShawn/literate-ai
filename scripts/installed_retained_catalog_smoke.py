"""Qualify retained catalog exclusion through an actual installed public CLI."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.component_lock_planning import (
    component_catalog_manifest_paths,
    observe_flavor_catalog,
)
from literate_ai.adapters.harness_inventory import (
    HARNESS_BASELINE_SCHEMA,
    HARNESS_PARITY_SCHEMA,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    observe_installed_framework_distribution,
)
from literate_ai.project_authority_graph import project_authority_graph
from literate_ai.projects import ProjectConfigurationStore, load_project


def fixture_snapshot(project: Path) -> dict:
    """Bounded physical/content observation of our owned fixture, excluding Git."""
    result = {}
    total = 0
    deadline = time.monotonic() + 30
    for directory, directories, filenames in os.walk(project, followlinks=False):
        if Path(directory) == project:
            directories[:] = [name for name in directories if name != ".git"]
        for name in directories + filenames:
            if len(result) >= 20_000 or time.monotonic() > deadline:
                raise RuntimeError("installed host fixture snapshot exceeds its budget")
            path = Path(directory) / name
            observed = path.lstat()
            entry = {
                "device": observed.st_dev,
                "inode": observed.st_ino,
                "mode": observed.st_mode,
                "mtime_ns": observed.st_mtime_ns,
            }
            if stat.S_ISLNK(observed.st_mode):
                entry["link"] = os.readlink(path)
            elif stat.S_ISREG(observed.st_mode):
                total += observed.st_size
                if observed.st_size > 64 * 1024**2 or total > 512 * 1024**2:
                    raise RuntimeError("installed host fixture bytes exceed its budget")
                entry.update(
                    bytes=observed.st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            elif not stat.S_ISDIR(observed.st_mode):
                raise RuntimeError("installed host fixture has an unsupported input")
            result[path.relative_to(project).as_posix()] = entry
    return result


def selected_shim_status(project: Path, invoke) -> dict:
    """Configure our fixture, then observe public status without writes."""
    selector = "+flavor://legacy-adoption/build-legacy-shim"
    shim = project / "flavors/legacy-project-shim/flavor.md"
    definition = load_project(project)
    matches = [
        item
        for item in observe_flavor_catalog(definition.roots("flavor"), boundary=project)
        if item.definition.coordinate.uri
        == "flavor://legacy-adoption/build-legacy-shim"
    ]
    if len(matches) != 1 or matches[0].path != shim:
        raise RuntimeError(
            "installed fixture lacks its differently named canonical shim"
        )
    if shim.with_name("host-toolchain.cdx.json").exists():
        raise RuntimeError("installed local shim unexpectedly declares host probes")

    def status() -> tuple[dict, dict]:
        before = fixture_snapshot(project)
        result = invoke("status", "--project", str(project), observe_host=True)
        after = fixture_snapshot(project)
        tools = result["host"]["host_tools"]
        if before != after:
            raise RuntimeError("installed public status changed adopted fixture inputs")
        if tools.get("error") or not isinstance(tools.get("ready"), bool):
            raise RuntimeError(
                f"installed public status could not resolve host tools: {tools}"
            )
        python = [
            item for item in tools["capabilities"] if item["capability"] == "python"
        ]
        if len(python) != 1:
            raise RuntimeError(
                "installed status suppressed the built-in Python requirement"
            )
        return tools, {
            "input_nodes": len(before),
            "snapshot_sha256": hashlib.sha256(
                json.dumps(before, sort_keys=True).encode()
            ).hexdigest(),
            "inputs_unchanged": True,
        }

    original, original_snapshot = status()
    store = ProjectConfigurationStore(project)
    current = store.read()
    selectors = current.definition.default_flavor_selectors
    if (
        "+flavor://literate-ai/lang-python" not in selectors
        or selectors.count("+flavor://literate-ai/build-make") != 1
        or selector in selectors
    ):
        raise RuntimeError(
            "installed fixture defaults do not match explicit status setup"
        )
    # Authored fixture setup, after actual adoption; no generated evidence is edited.
    selected = tuple(
        selector if value == "+flavor://literate-ai/build-make" else value
        for value in selectors
    )
    store.update(
        current, replace(current.definition, default_flavor_selectors=selected)
    )
    observed, selected_snapshot = status()

    def requirements(tools):
        return [
            {key: item[key] for key in ("capability", "required_version", "reason")}
            for item in tools["capabilities"]
            if item["capability"] == "python"
        ]

    if (
        requirements(original) != requirements(observed)
        or original["target"] != observed["target"]
        or original["realization_sbom"] != observed["realization_sbom"]
    ):
        raise RuntimeError("installed local shim changed native or Python requirements")
    return {
        "selected_shim": selector,
        "physical_manifest": shim.relative_to(project).as_posix(),
        "explicit_authored_configuration_after_adoption": True,
        "original_status": original_snapshot,
        "selected_status": selected_snapshot,
        "native_target": observed["target"],
        "python_requirements": requirements(observed),
        "observed_host_tools_ready": observed["ready"],
        "host_tools_error": None,
    }


def adoption_evidence(project: Path, initialized: dict) -> dict:
    documents = {}
    for name, schema in (
        ("legacy-harness-baseline.json", HARNESS_BASELINE_SCHEMA),
        ("legacy-wrapper-parity.json", HARNESS_PARITY_SCHEMA),
        ("legacy-lift-shift.json", "literate-ai/legacy-lift-shift@1"),
    ):
        relative = f".literate/{name}"
        if relative not in initialized["created"]:
            raise RuntimeError("public adoption did not create its evidence record")
        content = (project / relative).read_bytes()
        if len(content) > 2 * 1024**2:
            raise RuntimeError("retained catalog fixture evidence exceeds its bound")
        document = json.loads(content)
        if document.get("schema") != schema or document.get("state") != "passed":
            raise RuntimeError("installed adoption evidence did not pass")
        documents[name] = document
    baseline = documents["legacy-harness-baseline.json"]
    parity = documents["legacy-wrapper-parity.json"]
    shifted = documents["legacy-lift-shift.json"]

    def identity(document: dict) -> str:
        # Match the exact retained harness/ADR identity format.
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

    baseline_identity = identity(baseline)
    parity_identity = identity(parity)
    if shifted["phase_11_parity_identity"] != parity_identity:
        raise RuntimeError("lift-shift does not identify the saved wrapper parity")
    adr_path = "docs/decisions/0001-legacy-project-lift-and-shift.md"
    if shifted["adr"] != adr_path:
        raise RuntimeError("lift-shift did not retain its expected authority record")
    adr = (project / adr_path).read_text(encoding="utf-8")
    if (
        f"Baseline evidence: `{baseline_identity}`" not in adr
        or f"Shim parity evidence: `{parity_identity}`" not in adr
    ):
        raise RuntimeError("lift-shift authority does not bind its saved evidence")
    expected_tests = {
        "state": "nonempty",
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "known_failed": 0,
    }
    if shifted["phase_12_parity"].get("schema") != HARNESS_PARITY_SCHEMA:
        raise RuntimeError("lift-shift phase 12 has an unexpected parity schema")
    for report in (baseline, parity, shifted["phase_12_parity"]):
        if report.get("state") != "passed":
            raise RuntimeError("installed adoption parity did not pass")
        phases = {phase["phase"]: phase for phase in report["phases"]}
        if not {"build", "test", "package"} <= phases.keys():
            raise RuntimeError("installed adoption omitted a fixture phase")
        if any(
            phase["exit_code"] != 0 or phase["timed_out"] for phase in phases.values()
        ):
            raise RuntimeError("installed adoption phase failed or timed out")
        if phases["test"]["test_collection"] != expected_tests:
            raise RuntimeError("installed adoption did not run its real unittest")
        if report is not baseline and any(
            phase["parity"] is not True for phase in phases.values()
        ):
            raise RuntimeError("installed wrapper parity refused its source or tests")
    return {
        "baseline": baseline_identity,
        "wrapper_parity": parity_identity,
        "lift_shift": identity(shifted),
    }


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    expected_distribution = sys.argv[2]
    parent_source = sys.argv[3]
    if os.name != "posix":
        raise RuntimeError("the retained catalog fixture requires POSIX Make")
    distribution = observe_installed_framework_distribution()
    if distribution.identity.uri != expected_distribution:
        raise RuntimeError(
            "retained catalog fixture requires its exact installed wheel"
        )
    root.mkdir()
    project = root / "project"
    project.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    operator_provider_environment = {
        key: value
        for key, value in environment.items()
        if key in {"CODING_CLI", "LITAI_CODING_PROVIDER"}
    }
    environment.update(
        BUILD_DIR=str(root / "build"),
        OBJ_DIR=str(root / "obj"),
        CODING_CLI="no-generation-provider",
        LITAI_CODING_PROVIDER="coding-cli",
    )

    def run(
        *arguments: str, cwd: Path = root, environment_override: dict | None = None
    ) -> subprocess.CompletedProcess:
        try:
            return run_with_tree_kill(
                arguments,
                cwd=cwd,
                env=environment
                if environment_override is None
                else environment_override,
                timeout=300,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            for name, output in (("stdout", exc.stdout), ("stderr", exc.stderr)):
                (root / f"failed-command.{name}").write_bytes(
                    output if isinstance(output, bytes) else (output or "").encode()
                )
            detail = exc.stdout or exc.stderr or ""
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            raise RuntimeError(
                "installed retained catalog command failed; exact output retained "
                f"in {root}: {detail[-2000:]}"
            ) from exc

    def invoke(*arguments: str, observe_host: bool = False) -> dict:
        # The adoption-only unknown-provider guard is not a real host selection.
        # Observe the host's normal declared defaults, never force tool readiness.
        observed_environment = {
            key: value
            for key, value in environment.items()
            if key not in {"CODING_CLI", "LITAI_CODING_PROVIDER"}
        }
        observed_environment.update(operator_provider_environment)
        completed = run(
            sys.executable,
            "-I",
            "-m",
            "literate_ai.cli",
            "--json",
            *arguments,
            environment_override=observed_environment if observe_host else None,
        )
        envelope = json.loads(completed.stdout)
        if envelope.get("ok") is not True:
            raise RuntimeError(f"installed retained catalog command failed: {envelope}")
        return envelope["result"]

    (project / "app.py").write_text("def value():\n    return 7\n", encoding="utf-8")
    (project / "test_app.py").write_text(
        "import unittest\nfrom app import value\n"
        "class ValueTests(unittest.TestCase):\n"
        "    def test_value(self): self.assertEqual(value(), 7)\n",
        encoding="utf-8",
    )
    python = shlex.quote(sys.executable)
    (project / "Makefile").write_text(
        f"build:\n\t{python} -m py_compile app.py\n"
        f"test:\n\t{python} -m unittest discover -p 'test_*.py'\n"
        f"package:\n\t{python} -c 'import zipfile; "
        'z=zipfile.ZipFile("package.zip", "w"); z.write("app.py"); z.close()'
        "'\n",
        encoding="utf-8",
    )
    (project / ".gitignore").write_text(
        "node_modules/\n__pycache__/\npackage.zip\n", encoding="utf-8"
    )
    run("git", "init", "-b", "main", cwd=project)
    run("git", "add", ".", cwd=project)
    run(
        "git",
        "-c",
        "user.name=Retained Catalog Fixture",
        "-c",
        "user.email=retained-catalog@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Create retained catalog fixture",
        cwd=project,
    )
    dependencies = {
        "node_modules/extend/component.json": b'{"name":"third-party-component-io"}\n',
        "node_modules/markdown/component.md": b"Unrelated third-party Markdown\n",
        "node_modules/lock/component.lock.json": b"not a framework lock\n",
    }
    for name, content in dependencies.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    arguments = (
        "onboard",
        "adopt",
        str(project),
        "--from",
        parent_source,
        "--source-intelligence-provider",
        "none",
        "--default-branch",
        "main",
        "--run-baseline",
    )
    plan = invoke(*arguments)
    if plan.get("writes") is not False:
        raise RuntimeError("installed adoption plan was not read-only")
    applied = invoke(
        *arguments,
        "--apply",
        "--acknowledge",
        "--expect-plan",
        plan["plan_identity"],
    )
    initialized = applied.get("initialization", {})
    if applied.get("applied") is not True:
        raise RuntimeError("installed adoption was not applied")
    evidence = adoption_evidence(project, initialized)
    retained = project / "components/legacy-project-wrapper/implementation"
    for name, content in dependencies.items():
        if (retained / name).read_bytes() != content:
            raise RuntimeError("installed adoption changed a retained dependency")
    validated = invoke("project", "validate", str(project))
    manifests = component_catalog_manifest_paths(
        (project / "components", project / "samples"), boundary=project
    )
    if not manifests or any(path.is_relative_to(retained) for path in manifests):
        raise RuntimeError(
            "retained dependencies entered installed Component discovery"
        )
    graph = project_authority_graph(project).to_dict()
    host_status = selected_shim_status(project, invoke)
    if observe_installed_framework_distribution().identity != distribution.identity:
        raise RuntimeError("installed framework changed during catalog qualification")
    print(
        json.dumps(
            {
                "qualified": True,
                "distribution": distribution.identity.uri,
                "public_adopt_applied": True,
                "actual_unittest_baseline_and_parity": True,
                "adoption_evidence": evidence,
                "dependency_bytes_preserved": {
                    name: hashlib.sha256(content).hexdigest()
                    for name, content in dependencies.items()
                },
                "validated_components": validated["components"],
                "catalog_manifests": len(manifests),
                "authority_graph_sha256": hashlib.sha256(
                    json.dumps(graph, sort_keys=True).encode()
                ).hexdigest(),
                "selected_local_shim_host_status": host_status,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
