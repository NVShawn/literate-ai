"""Qualify public retained continuation with real installed distribution metadata."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import literate_ai
from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
    record_project_authority_review,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    observe_installed_framework_distribution,
)
from literate_ai.contracts import (
    RepositoryParentSelection,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
)
from literate_ai.projects import ProjectConfigurationStore

COMPONENT = "samples/hello-component"


def inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def invoke(project: Path, *arguments: str, error: str | None = None) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "literate_ai.cli",
            "--json",
            *arguments,
            "--project",
            str(project),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    try:
        result = json.loads(completed.stdout or completed.stderr)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"installed continuation returned non-JSON: {completed.stderr[-2000:]}"
        ) from exc
    if error is not None:
        if completed.returncode == 0 or result.get("error", {}).get("code") != error:
            raise RuntimeError(f"expected {error}: {result}")
    elif completed.returncode != 0 or not result.get("ok"):
        raise RuntimeError(f"installed continuation failed: {result}")
    return result


def scenario(root: Path, *, declared: bool, author_fixture) -> dict:
    root.mkdir()
    project = root / "project"
    project.mkdir()
    os.environ.update(
        BUILD_DIR=str(root / "build"),
        OBJ_DIR=str(root / "obj"),
        # Expected bytecode is authored by this installed interpreter. Select
        # that same runtime through the public operator binding rather than
        # allowing a different supported Python on PATH to change its format.
        PYTHON=sys.executable,
        CODING_CLI="no-generation-provider",
        LITAI_CODING_PROVIDER="coding-cli",
    )
    # Default owners observe the installed wheel; no injected distribution,
    # lifecycle binding, acceptance, model provider or receipt implementation.
    FilesystemProjectInitializationAdapter().initialize(
        project,
        parent_selection=RepositoryParentSelection.root(),
        source_intelligence_provider="none",
    )
    cache = root / "build/accepted-source-cache"
    if declared:
        store = ProjectConfigurationStore(project)
        snapshot = store.read()
        store.update(
            snapshot,
            replace(
                snapshot.definition,
                source_cache=SourceCacheConfiguration(
                    mode=SourceCacheMode.READ_WRITE,
                    targets=(
                        SourceCacheTarget(
                            target_id="project-committed",
                            root_kind=SourceCacheRootKind.PROJECT_RELATIVE,
                            root_reference="generated/committed-source-cache",
                        ),
                    ),
                    write_target_id="project-committed",
                    require_unique=True,
                ),
            ),
        )
        record_project_authority_review(project)
        cache = project / "generated/committed-source-cache"
    source = root / "retained"
    author_fixture(project, source)
    identities, expected = [], []
    previous = {}
    for revision in range(2):
        if revision:
            with (source / "main.py").open("a", encoding="utf-8") as stream:
                stream.write("\n# independently admitted second retained revision\n")
        compiled = root / f"expected-{revision}.pyc"
        py_compile.compile(
            str(source / "main.py"),
            cfile=str(compiled),
            dfile="main.py",
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
        expected.append(compiled.read_bytes())
        rebuild = (
            "rebuild",
            COMPONENT,
            "--allow-host-execution",
            "--update-receipt",
            "--retained-source",
            str(source),
        )
        plan = invoke(project, *rebuild, "--retained-source-plan")
        admitted = invoke(
            project,
            *rebuild,
            "--authorize-retained-source",
            plan["result"]["authorization_identity"],
        )["result"]
        if not admitted["passed"] or not admitted["receipt_committed"]:
            raise RuntimeError("installed retained admission did not commit success")
        published = admitted["source_cache_published_entry_identities"]
        if len(published) != 1 or published[0] in identities:
            raise RuntimeError("installed admission omitted a distinct publication")
        identities.extend(published)
        current = inventory(cache)
        if not current or any(current.get(k) != v for k, v in previous.items()):
            raise RuntimeError("successive admission changed historical cache bytes")
        previous = current
        if revision == 0:
            invoke(project, "build", COMPONENT, "--from-accepted-source")
    receipt = project / "verification/current.json"
    committed = receipt.read_bytes()
    invoke(
        project,
        "build",
        COMPONENT,
        "--from-accepted-source",
        error="execution.accepted_source_provider_ambiguous",
    )
    invoke(
        project,
        "build",
        COMPONENT,
        "--from-accepted-source",
        "--source-cache-entry",
        "sha256:" + "0" * 64,
        error="source-cache.entry-not-found",
    )
    if receipt.read_bytes() != committed:
        raise RuntimeError("refused installed continuation changed the receipt")
    for index in (1, 0):
        built = invoke(
            project,
            "build",
            COMPONENT,
            "--from-accepted-source",
            "--source-cache-entry",
            identities[index],
        )["result"]
        if (
            not built["passed"]
            or Path(built["artifact"]).read_bytes() != expected[index]
        ):
            raise RuntimeError("installed exact selection exported different bytes")
        payload = {"name": "Ada"}
        if "| `messages` |" in (project / COMPONENT / "component.md").read_text():
            payload["messages"] = ["hello", "world"]
        ran = invoke(project, "run", COMPONENT, json.dumps([payload]))["result"]
        if ran["exit_status"] != 0:
            raise RuntimeError("installed accepted-source run failed")
    if inventory(cache) != previous:
        raise RuntimeError("installed continuation changed retained cache bytes")
    return {"admissions": 2, "exact_builds": 2, "runs": 2, "history_unchanged": True}


def main() -> int:
    root, expected_distribution = Path(sys.argv[1]).resolve(), sys.argv[2]
    repository = Path(__file__).resolve().parents[1]
    if Path(literate_ai.__file__).resolve().is_relative_to(repository):
        raise RuntimeError("installed continuation imported the source checkout")
    observed = observe_installed_framework_distribution()
    if observed.identity.uri != expected_distribution:
        raise RuntimeError(
            "installed continuation distribution differs from wheel smoke"
        )
    # Only untrusted fixture authoring is imported from the test tree. This is
    # a src-layout repository: never add repository/src to the installed process.
    sys.path.insert(0, str(repository))
    try:
        from tests.support.fixtures_retained_native import (
            author_retained_native_fixture,
        )
    finally:
        del sys.path[0]
    root.mkdir()
    results = {
        name: scenario(
            root / name,
            declared=declared,
            author_fixture=author_retained_native_fixture,
        )
        for name, declared in (("default", False), ("project-relative", True))
    }
    if observe_installed_framework_distribution().identity != observed.identity:
        raise RuntimeError("installed continuation changed the framework distribution")
    print(
        json.dumps(
            {"distribution": observed.identity.uri, "scenarios": results},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
