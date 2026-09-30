"""Compile and observe reviewed tests inside an existing guarded consumer run.

These observations are not durable admission receipts. The surrounding execution
session owns authorization, measured compilers, input custody and process bounds.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

from literate_ai._filesystem import ensure_safe_directory, require_safe_directory
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.retained_cargo_build_environment import (
    cargo_build_environments,
)
from literate_ai.adapters.retained_cargo_build_outputs import (
    capture_retained_cargo_build_outputs,
)
from literate_ai.adapters.retained_cargo_runtime_files import (
    capture_retained_cargo_runtime_files,
)
from literate_ai.adapters.retained_cargo_test_observation import (
    select_cargo_test_executables,
    verify_libtest_observation,
)
from literate_ai.adapters.retained_native_dependencies import (
    observe_retained_native_dependencies,
)
from literate_ai.adapters.retained_package_tree import _signature
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.projects import PinnedInputClosure


def observe_retained_cargo_tests(
    authority, metadata, cargo, *, rustc, environment, offline, run
):
    """Use the session's guarded process runner and retain failed outputs there.

    The fresh output directory is retained on both success and failure. Direct
    binaries use captured compiler/generated library paths with the caller's
    explicit environment. Caller paths and system-loader authority require
    separate qualification before public consumer admission.
    """
    authority.require_unchanged()
    plan = authority.materialized.plan
    workspace = authority.importer.project.root / plan.workspace_root
    output = workspace / plan.graph.output_directory
    ensure_safe_directory(output)
    parent_node = directory_node(output)
    fresh = Path(tempfile.mkdtemp(prefix="tests-", dir=output))
    fresh_node = directory_node(fresh)
    compiler_runtime = None

    def output_guard():
        authority.require_unchanged()
        if compiler_runtime is not None:
            compiler_runtime.require_unchanged()
        require_safe_directory(output)
        require_safe_directory(fresh)
        if directory_node(output) != parent_node or directory_node(fresh) != fresh_node:
            raise ValueError("retained.tests.output-replaced")

    argv = [
        "cargo",
        "test",
        "--workspace",
        "--all-targets",
        "--no-run",
        "--locked",
        "--message-format=json",
        "--target",
        plan.target,
        "--target-dir",
        str(fresh),
    ]
    if offline:
        argv.append("--offline")
    if plan.features:
        argv.append("--features=" + ",".join(plan.features))
    if plan.all_features:
        argv.append("--all-features")
    if plan.no_default_features:
        argv.append("--no-default-features")
    libdir_result = run(
        RepositoryBuildCommand(
            "retained-test-runtime",
            ("rustc", "--print", "target-libdir", "--target", plan.target),
            plan.workspace_root,
            (),
            network=False,
        ),
        rustc,
        extra_guard=output_guard,
    )
    lines = libdir_result.stdout.decode("utf-8").splitlines()
    if len(lines) != 1 or not lines[0] or libdir_result.stderr:
        raise ValueError("retained.runtime.compiler-path-invalid")
    compiler_runtime = capture_retained_cargo_runtime_files(
        (Path(lines[0]),), file_alias_roots=(Path(lines[0]),)
    )
    compiled = run(
        RepositoryBuildCommand(
            "retained-test-compile",
            tuple(argv),
            plan.workspace_root,
            (),
            network=not offline,
        ),
        cargo,
        extra_guard=output_guard,
    )
    binaries = select_cargo_test_executables(
        compiled,
        metadata,
        workspace_root=workspace,
        output_root=fresh,
        expected=plan.graph,
    )
    records = tuple(json.loads(line) for line in compiled.stdout.splitlines())
    build_environments = cargo_build_environments(
        records, metadata, workspace=workspace
    )
    build_outputs = capture_retained_cargo_build_outputs(
        records, metadata, output_root=fresh
    )
    runtime_roots = []
    known_packages = {package["id"] for package in metadata["packages"]}
    for record in records:
        if record.get("reason") != "build-script-executed":
            continue
        if record.get("package_id") not in known_packages or not isinstance(
            record.get("linked_paths"), list
        ):
            raise ValueError("retained.runtime.build-script-invalid")
        for raw in record["linked_paths"]:
            if not isinstance(raw, str):
                raise ValueError("retained.runtime.search-path-invalid")
            if raw.partition("=")[0] in {
                "dependency",
                "crate",
                "native",
                "framework",
                "all",
            }:
                raw = raw.partition("=")[2]
            path = Path(raw)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("retained.runtime.search-path-invalid")
            if path.is_relative_to(fresh):
                runtime_roots.append(path)
    runtime_base = fresh / plan.target / "debug"
    runtime_roots.extend((runtime_base / "deps", runtime_base, Path(lines[0])))
    runtime = capture_retained_cargo_runtime_files(
        runtime_roots, file_alias_roots=(Path(lines[0]),)
    )
    compiler_runtime.require_unchanged()
    compiler_runtime = None
    expected = {target.key: target.cases for target in authority.inventory.targets}
    closure = PinnedInputClosure(
        maximum_files=4096,
        maximum_file_bytes=64 * 1024 * 1024,
        maximum_total_bytes=256 * 1024 * 1024,
    )
    nodes, parents, identities = {}, {}, {}
    for binary in binaries:
        path = binary.executable
        node = _signature(path.lstat())
        content = closure.pin(
            path, boundary=fresh, label=path.relative_to(fresh).as_posix()
        )
        if _signature(path.lstat()) != node:
            raise ValueError("retained.tests.binary-changed")
        nodes[path] = node
        parent = path.parent
        while parent != fresh:
            require_safe_directory(parent)
            parents[parent] = directory_node(parent)
            parent = parent.parent
        identities[path] = canonical_identity(
            {
                "schema": "literate-ai/retained-cargo-test-executable-observation@1",
                "inventory": authority.inventory.identity.uri,
                "runtime": runtime.identity.uri,
                "build_outputs": build_outputs.identity.uri,
                "build_environment": list(
                    build_environments.get(binary.package_root, ())
                ),
                "path": path.relative_to(fresh).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )

    def binary_guard():
        output_guard()
        build_outputs.require_unchanged()
        runtime.require_unchanged()
        closure.require_unchanged()
        if any(_signature(p.lstat()) != node for p, node in nodes.items()) or any(
            directory_node(p) != node for p, node in parents.items()
        ):
            raise ValueError("retained.tests.binary-changed")
        closure.require_unchanged()

    binary_guard()
    for index, binary in enumerate(binaries):
        key = (
            binary.package_root,
            binary.target_name,
            tuple(sorted(binary.target_kinds)),
        )
        package = workspace / binary.package_root
        build_environment = build_environments.get(binary.package_root, ())
        tool_environment = {
            **dict(build_environment),
            "CARGO_MANIFEST_DIR": str(package),
            **dict(runtime.environment({**environment, **dict(build_environment)})),
        }
        native = observe_retained_native_dependencies(
            artifact_root=fresh,
            artifact_files=(binary.executable,),
            root_ref=identities[binary.executable].uri,
            library_roots=runtime.roots,
            windows_environment=(
                {**environment, **tool_environment}
                if sys.platform in {"win32", "cygwin"}
                else None
            ),
            macos_environment=(
                {**environment, **tool_environment}
                if sys.platform == "darwin"
                else None
            ),
            linux_environment=(
                {**environment, **tool_environment}
                if sys.platform.startswith("linux")
                else None
            ),
        )

        def target_guard(native=native):
            binary_guard()
            native.require_unchanged()
            binary_guard()

        tool = LocalComponentToolBinding(
            str(binary.executable),
            (),
            canonical_identity(
                {
                    "schema": "literate-ai/retained-cargo-native-test-authority@1",
                    "executable": identities[binary.executable].uri,
                    "native_dependencies": native.identity.uri,
                }
            ),
            tuple(sorted(tool_environment.items())),
            target_guard,
        )
        results = []
        for phase, arguments in (
            ("list", ("--list", "--format=terse")),
            ("run", ("--format=pretty", "--color=never")),
        ):
            results.append(
                run(
                    RepositoryBuildCommand(
                        f"retained-test-{index}-{phase}",
                        (str(binary.executable), *arguments),
                        package.relative_to(authority.importer.project.root).as_posix(),
                        (),
                        network=False,
                    ),
                    tool,
                    extra_guard=target_guard,
                )
            )
        verify_libtest_observation(*results, expected[key])
    binary_guard()
