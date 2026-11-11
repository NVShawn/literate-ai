"""Offline native execution for an originally loaded Rust v2 verifier.

This is an internal Standard acceptance adapter. It does not admit a library,
restore a source owner, install dependencies or grant host execution permission.
The calling lifecycle retains its exact locked export and execution policy.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from literate_ai._filesystem import ensure_safe_directory, require_safe_directory
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.cargo import (
    DEFAULT_CARGO_VERSION_TIMEOUT_SECONDS,
    CargoToolchain,
)
from literate_ai.adapters.builders.cargo import (
    _identity as cargo_identity,
)
from literate_ai.adapters.builders.rust import (
    DEFAULT_RUST_VERSION_TIMEOUT_SECONDS,
    RustToolchain,
    _rust_toolchain_identity,
)
from literate_ai.adapters.component_acceptance import ComponentAcceptanceError
from literate_ai.adapters.retained_cargo_execution_inputs import _state
from literate_ai.adapters.rust_library_acceptance import (
    RustLibraryAcceptance,
    capture_loaded_rust_library_verifier_inputs,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.rust_library_harness import (
    RustHarnessResolution,
    RustHarnessResolvedDependency,
    RustHarnessResolvedPackage,
)
from literate_ai.diagnostics import redact_secrets
from literate_ai.projects import PinnedInputClosure
from literate_ai.storage.cas import FileSystemCAS, canonical_json_bytes


def _refuse(reason):
    raise ComponentAcceptanceError(
        "component_acceptance.rust_harness_" + reason,
        "The current offline Rust verifier inputs or native observation differ",
    )


def _object_pairs(rows):
    value = dict(rows)
    if len(value) != len(rows):
        _refuse("duplicate_field")
    return value


def _json(raw):
    def nonfinite(_value):
        _refuse("nonfinite_json")

    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=_object_pairs, parse_constant=nonfinite
    )


def _require_results(raw, oracle):
    from literate_ai.contracts.product_json import product_json_values_equal

    result = _json(raw)
    if (
        type(result) is not dict
        or set(result) != {"schema", "cases"}
        or result["schema"] != "literate-ai/library-acceptance-results@1"
        or type(result["cases"]) is not list
        or len(result["cases"]) != len(oracle.cases)
    ):
        _refuse("result_protocol")
    observed = {}
    for case in result["cases"]:
        if (
            type(case) is not dict
            or set(case) != {"case_id", "capability", "result"}
            or type(case["case_id"]) is not str
            or case["case_id"] in observed
        ):
            _refuse("result_protocol")
        observed[case["case_id"]] = case
    if set(observed) != {case.case_id for case in oracle.cases} or any(
        observed[case.case_id]["capability"] != case.capability
        or not product_json_values_equal(
            observed[case.case_id]["result"], case.expected_result
        )
        for case in oracle.cases
    ):
        _refuse("result_mismatch")


def _manifest(oracle, *, library_alias, library_directory):
    declaration = oracle.rust_harness
    declaration.require_library_alias(library_alias)
    library = next(
        row
        for row in declaration.resolution.packages
        if row.identity == declaration.resolution.library
    )
    # Explicit features, including `default` when reviewed, avoid an implicit
    # candidate-manifest default selection. Cargo's actual graph must agree.
    dependencies = {
        library_alias: {
            "package": library.name,
            "path": str(library_directory),
            "default-features": False,
            "features": list(library.features),
        }
    }
    for row in declaration.dependencies:
        dependencies[row.alias] = {
            "package": row.package,
            "version": "=" + row.version,
            "registry": row.registry,
            "default-features": row.default_features,
            "features": list(row.features),
        }
    lines = [
        "[package]",
        'name="litai-library-acceptance"',
        'version="0.0.0"',
        'edition="2021"',
        "[workspace]",
        "[dependencies]",
    ]
    for alias, values in sorted(dependencies.items()):
        members = []
        for name, value in values.items():
            members.append(
                json.dumps(name) + "=" + json.dumps(value, ensure_ascii=False)
            )
        lines.append(json.dumps(alias) + "={" + ",".join(members) + "}")
    return ("\n".join(lines) + "\n").encode()


def _require_source_configuration(workspace, home, roots):
    """Accept source routing, never a config-selected compiler or wrapper.

    The complete original roots retain byte/topology custody independently.
    Native metadata must still map every actually selected crate to those roots.
    """
    pinned = PinnedInputClosure(
        maximum_files=4, maximum_file_bytes=1024**2, maximum_total_bytes=4 * 1024**2
    )
    for directory in (workspace / ".cargo", home):
        files = [
            directory / name
            for name in ("config", "config.toml")
            if os.path.lexists(directory / name)
        ]
        if len(files) > 1:
            _refuse("configuration_ambiguity")
        for path in files:
            raw = pinned.pin(path, boundary=directory, label=str(path))
            document = tomllib.loads(raw.decode())
            if set(document) - {"source", "registries", "net"}:
                _refuse("configuration_override")
            if document.get("net", {}) not in ({}, {"offline": True}):
                _refuse("configuration_network")
            sources = document.get("source", {})
            registries = document.get("registries", {})
            if type(sources) is not dict or type(registries) is not dict:
                _refuse("configuration_source")
            for row in registries.values():
                if (
                    type(row) is not dict
                    or set(row) != {"index"}
                    or type(row["index"]) is not str
                ):
                    _refuse("configuration_registry")
            for row in sources.values():
                if (
                    type(row) is not dict
                    or not row
                    or set(row)
                    - {
                        "directory",
                        "registry",
                        "replace-with",
                    }
                ):
                    _refuse("configuration_source")
                if "replace-with" in row and row["replace-with"] not in sources:
                    _refuse("configuration_source")
                if "directory" in row:
                    if type(row["directory"]) is not str:
                        _refuse("configuration_source")
                    source = Path(row["directory"])
                    if not source.is_absolute():
                        source = directory.parent / source
                    source = source.resolve(strict=True)
                    if not any(source.is_relative_to(root) for _, root in roots):
                        _refuse("configuration_source")
    pinned.require_unchanged()
    return pinned


def observe_rust_harness_resolution(
    raw, *, declaration, harness, export, roots, current_paths=True
):
    """Project actual Cargo identities to exact captured, portable owners.

    The caller must separately retain the raw native command and live source,
    library, compiler and lock custody. A graph value alone grants no execution.
    """
    data = _json(raw)
    expected = declaration.resolution
    if (
        type(data) is not dict
        or type(data.get("version")) is not int
        or data["version"] != 1
    ):
        _refuse("metadata_invalid")
    packages, resolve = data.get("packages"), data.get("resolve")
    if (
        type(packages) is not list
        or len(packages) != len(expected.packages)
        or type(resolve) is not dict
        or type(resolve.get("nodes")) is not list
        or len(resolve["nodes"]) != len(packages)
        or data.get("workspace_root") != str(harness)
        or data.get("target_directory") != str(harness / "target")
        or data.get("build_directory", str(harness / "target"))
        != str(harness / "target")
    ):
        _refuse("metadata_membership")
    selected, nodes = {}, {}
    coordinate_path = (
        Path
        if current_paths
        else (PureWindowsPath if PureWindowsPath(str(harness)).drive else PurePosixPath)
    )
    harness, export = coordinate_path(harness), coordinate_path(export)
    roots = tuple((name, coordinate_path(root)) for name, root in roots)
    owners = (
        ("harness", harness),
        ("library", export),
        *(("input:" + name, root) for name, root in roots),
    )
    for package in packages:
        if type(package) is not dict or type(package.get("id")) is not str:
            _refuse("metadata_package")
        coordinate = package["id"]
        path = coordinate_path(package.get("manifest_path", ""))
        if (
            not path.is_absolute()
            or ".." in path.parts
            or (current_paths and path.resolve(strict=True) != path)
        ):
            _refuse("metadata_path")
        matches = [(scope, root) for scope, root in owners if path.is_relative_to(root)]
        if len(matches) != 1 or coordinate in selected:
            _refuse("metadata_owner")
        scope, root = matches[0]
        selected[coordinate] = RustHarnessResolvedPackage(
            scope,
            path.relative_to(root).as_posix(),
            package.get("name"),
            package.get("version"),
            package.get("source"),
            (),
            (),
        )
    for node in resolve["nodes"]:
        if (
            type(node) is not dict
            or node.get("id") not in selected
            or node["id"] in nodes
        ):
            _refuse("metadata_node")
        nodes[node["id"]] = node
    if set(nodes) != set(selected) or resolve.get("root") not in selected:
        _refuse("metadata_node")
    edge_limit = sum(len(row.dependencies) for row in expected.packages)
    edges_used, projected = 0, []
    for coordinate, package in selected.items():
        node = nodes[coordinate]
        features, dependencies = node.get("features"), node.get("deps")
        if type(features) is not list or type(dependencies) is not list:
            _refuse("metadata_edges")
        edges = []
        for edge in dependencies:
            if type(edge) is not dict or edge.get("pkg") not in selected:
                _refuse("metadata_edges")
            kinds = edge.get("dep_kinds")
            if type(kinds) is not list or not kinds or len(kinds) > edge_limit:
                _refuse("metadata_edges")
            edges_used += len(kinds)
            if edges_used > edge_limit:
                _refuse("metadata_edges")
            for kind in kinds:
                if type(kind) is not dict or set(kind) != {"kind", "target"}:
                    _refuse("metadata_edges")
                edges.append(
                    RustHarnessResolvedDependency(
                        edge.get("name"),
                        selected[edge["pkg"]].identity,
                        kind["kind"],
                        kind["target"],
                    )
                )
        projected.append(
            replace(
                package,
                features=tuple(sorted(features)),
                dependencies=tuple(sorted(edges, key=lambda edge: edge.key)),
            )
        )
    result = RustHarnessResolution(
        selected[resolve["root"]].identity,
        expected.library,
        tuple(sorted(projected, key=lambda row: row.identity.uri)),
    )
    if result != expected:
        _refuse("resolution_drift")
    return result


@contextmanager
def execute_rust_library_harness(
    oracle,
    *,
    stage,
    export,
    library_manifest,
    library_alias,
    cargo,
    rustc,
    cases_document,
    surface_document,
    environment,
    require_library_current,
    record_bytes,
    evidence_root,
    allow_host_execution,
):
    """Run only under the caller's current Standard acceptance authorization."""
    if allow_host_execution is not True or type(oracle) is not RustLibraryAcceptance:
        _refuse("authority_unavailable")
    declaration = oracle.rust_harness
    cargo_owner = getattr(cargo._authority_guard, "__self__", None)
    rust_owner = getattr(rustc._authority_guard, "__self__", None)
    if (
        type(cargo_owner) is not CargoToolchain
        or type(rust_owner) is not RustToolchain
        or cargo.authority_identity is None
        or rustc.authority_identity is None
        or not callable(cargo._authority_guard)
        or not callable(rustc._authority_guard)
        or cargo.command != cargo_owner.command
        or rustc.command != rust_owner.command
        or cargo.toolchain_identity != declaration.cargo_toolchain
        or rustc.toolchain_identity != declaration.rust_toolchain
        or cargo.arguments
        or rustc.arguments
    ):
        _refuse("tool_mismatch")
    expires = time.monotonic() + declaration.timeout_seconds
    commands = []
    with capture_loaded_rust_library_verifier_inputs(oracle) as inputs:
        roots = inputs.roots
        selected = dict(roots)
        # No output or sealed library is adopted as a provisioned input owner.
        if any(
            path.is_relative_to(root) or root.is_relative_to(path)
            for _, root in roots
            for path in (stage, export, evidence_root)
        ):
            _refuse("input_overlap")
        if any(
            evidence_root.is_relative_to(path) or path.is_relative_to(evidence_root)
            for path in (stage, export)
        ):
            _refuse("evidence_overlap")
        # This is diagnostic storage in the lifecycle's existing object root,
        # never a verifier input, source owner or execution authorization.
        require_safe_directory(evidence_root, allow_missing=True)
        ensure_safe_directory(evidence_root)
        evidence = FileSystemCAS(evidence_root)

        def retain(content):
            reference = evidence.put_bytes(content)
            identity = ContentIdentity.parse_uri("sha256:" + reference.digest)
            if record_bytes(content) != identity:
                _refuse("evidence_identity")
            return identity

        workspace, home = selected["workspace"], selected["cargo-home"]
        configuration = _require_source_configuration(workspace, home, roots)
        # A normal home .cargo directory is not ambient configuration by itself.
        # Pin the actual config coordinates (and their safe parent states), not
        # unrelated registry/bin/cache siblings in that directory.
        ancestors = tuple(
            parent / ".cargo" / name
            for parent in workspace.parents
            for name in ("config", "config.toml")
        )
        absent = tuple((path, _state(path)) for path in ancestors)
        if any(state[2] is not None for _, state in absent):
            _refuse("ambient_configuration")
        values = {
            name: value
            for name, value in environment.items()
            if name
            in {
                "PATH",
                "SystemRoot",
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "LD_LIBRARY_PATH",
                "DYLD_LIBRARY_PATH",
            }
        }
        for tool in (cargo, rustc):
            values.update(dict(tool.environment))
        if any(name.upper().startswith(("CARGO_", "RUST")) for name in values):
            _refuse("tool_environment")
        values.update(
            {
                name.removeprefix("external:"): str(root)
                for name, root in roots
                if name.startswith("external:")
            }
        )
        if any(name.upper().startswith(("CARGO_", "RUST")) for name in values):
            _refuse("external_environment_override")
        values.update(
            CARGO_HOME=str(home),
            CARGO_TARGET_DIR=str(stage / "target"),
            CARGO_NET_OFFLINE="true",
            RUSTC=rustc.executable,
            HOME=str(stage),
            USERPROFILE=str(stage),
            TMPDIR=str(stage),
            TEMP=str(stage),
            TMP=str(stage),
        )
        if time.monotonic() >= expires:
            _refuse("deadline")
        manifest = _manifest(
            oracle,
            library_alias=library_alias,
            library_directory=library_manifest.parent,
        )
        (stage / "src").mkdir(mode=0o700)
        for path, content in (
            ("src/main.rs", oracle.harness_content),
            ("Cargo.toml", manifest),
            ("Cargo.lock", inputs.lockfile),
        ):
            with (stage / path).open("xb") as stream:
                stream.write(content)
        generated = PinnedInputClosure(
            maximum_files=3,
            maximum_file_bytes=16 * 1024**2,
            maximum_total_bytes=18 * 1024**2,
        )
        for path, content in (
            ("src/main.rs", oracle.harness_content),
            ("Cargo.toml", manifest),
            ("Cargo.lock", inputs.lockfile),
        ):
            generated.pin(
                stage / path, boundary=stage, label=path, expected_content=content
            )

        def current():
            inputs.require_current()
            require_library_current()
            cargo.require_unchanged()
            rustc.require_unchanged()

            # A route measured under the caller's home can become unusable or
            # select differently under isolated verifier inputs (notably rustup).
            # Reuse the genuine native observers with the ACTUAL child values;
            # never restore ambient selection files or substitute commands.
            def remaining():
                seconds = expires - time.monotonic()
                if seconds <= 0:
                    _refuse("deadline")
                return seconds

            cargo_owner.require_unchanged(
                values,
                timeout_seconds=min(DEFAULT_CARGO_VERSION_TIMEOUT_SECONDS, remaining()),
            )
            # Rust's canonical observer runs two bounded native subprobes.
            # Give each at most half the remaining operation budget, rather
            # than allowing two full default timeouts after the deadline.
            rust_owner.require_unchanged(
                values,
                timeout_seconds=min(
                    DEFAULT_RUST_VERSION_TIMEOUT_SECONDS, remaining() / 2
                ),
            )
            generated.require_unchanged()
            configuration.require_unchanged()
            if any(_state(path) != state for path, state in absent):
                _refuse("ambient_configuration")
            # Version/selection probes are real native commands too. Close the
            # source bracket after them before compiling any reviewed input.
            require_library_current()
            inputs.require_current()
            if time.monotonic() >= expires:
                _refuse("deadline")

        def run(argv, phase):
            current()
            result = run_bounded_process(
                argv,
                cwd=workspace,
                environment=values,
                timeout_seconds=expires - time.monotonic(),
                stdout_limit_bytes=declaration.stdout_limit_bytes,
                stderr_limit_bytes=declaration.stderr_limit_bytes,
                terminate_descendants=True,
                trace=False,
                error_prefix="component_acceptance.rust_" + phase,
            )
            current()
            commands.append(
                {
                    "phase": phase,
                    "argv": list(argv),
                    "environment_identity": canonical_identity(values).uri,
                    "exit_code": result.returncode,
                    "stdout_identity": retain(result.stdout).uri,
                    "stderr_identity": retain(result.stderr).uri,
                }
            )
            if result.returncode:
                failure = {
                    "schema": "literate-ai/rust-library-harness-failure@1",
                    "oracle_identity": oracle.identity.uri,
                    "declaration_identity": canonical_identity(
                        declaration.to_dict()
                    ).uri,
                    "verifier_inputs_identity": inputs.identity.uri,
                    "qualified": False,
                    "commands": list(commands),
                }
                failure_identity = retain(canonical_json_bytes(failure))
                detail = redact_secrets(
                    result.stderr[-4096:].decode("utf-8", errors="replace"),
                    {**environment, **values},
                )
                raise ComponentAcceptanceError(
                    "component_acceptance.rust_harness_native_command_failed",
                    f"Rust verifier {phase} exited {result.returncode}; "
                    f"retained failure {failure_identity.uri}: {detail}",
                )
            return result

        common = ("--offline", "--locked", "--manifest-path", str(stage / "Cargo.toml"))

        def metadata(phase):
            result = run(
                (*cargo.command, "metadata", "--format-version", "1", *common), phase
            )
            return observe_rust_harness_resolution(
                result.stdout,
                declaration=declaration,
                harness=stage,
                export=export,
                roots=roots,
            )

        resolution = metadata("metadata_before")
        run(
            (
                *cargo.command,
                "build",
                "--quiet",
                *common,
                "--target-dir",
                str(stage / "target"),
            ),
            "build",
        )
        metadata("metadata_after")
        executable = (
            stage
            / "target/debug"
            / (
                "litai-library-acceptance.exe"
                if os.name == "nt"
                else "litai-library-acceptance"
            )
        )
        native = PinnedInputClosure(
            maximum_files=1,
            maximum_file_bytes=256 * 1024**2,
            maximum_total_bytes=256 * 1024**2,
        )
        native_bytes = native.pin(executable, boundary=stage, label="verifier")
        native.require_unchanged()
        result = run(
            (str(executable), str(export), surface_document, cases_document), "execute"
        )
        try:
            _require_results(result.stdout, oracle)
        finally:
            # A semantic rejection is actionable only while the exact native
            # executable, tools and library remain current. Drift overrides a
            # mismatch rather than escaping these pins via the exception path.
            native.require_unchanged()
            current()
        observation = {
            "schema": "literate-ai/rust-library-harness-execution@1",
            "oracle_identity": oracle.identity.uri,
            "declaration_identity": canonical_identity(declaration.to_dict()).uri,
            "verifier_inputs_identity": inputs.identity.uri,
            "input_owners_identity": declaration.inputs.owners_identity.uri,
            "lockfile_identity": record_bytes(inputs.lockfile).uri,
            "manifest_identity": record_bytes(manifest).uri,
            "resolution": resolution.to_dict(),
            "cargo_toolchain_identity": cargo.toolchain_identity.uri,
            "rust_toolchain_identity": rustc.toolchain_identity.uri,
            "native_verifier_identity": record_bytes(native_bytes).uri,
            "execution_roots": {
                "harness": str(stage),
                "library": str(export),
                "inputs": [{"scope": name, "root": str(root)} for name, root in roots],
            },
            "cargo_command": list(cargo.command),
            "rustc_command": list(rustc.command),
            "cargo_observation": cargo_owner.to_dict(),
            "rust_observation": {
                "command": list(rust_owner.command),
                "launcher_executable": rust_owner.launcher_executable,
                "launcher_digest": rust_owner.launcher_digest,
                "compiler_executable": rust_owner.compiler_executable,
                "compiler_digest": rust_owner.compiler_digest,
                "version": rust_owner.version,
                "identity": rust_owner.identity,
            },
            "commands": commands,
        }
        yield result, observation
        native.require_unchanged()
        current()


def verify_rust_harness_execution(reader, identity, *, oracle, library_alias, surface):
    """Reopen exact v2 command assertions against current verifier custody.

    Historical paths are coordinates for pure graph comparison only. No archived
    value is passed to a command, used as a filesystem owner or restored as a grant.
    """
    document = reader.read_json(identity)
    fields = {
        "schema",
        "oracle_identity",
        "declaration_identity",
        "verifier_inputs_identity",
        "input_owners_identity",
        "lockfile_identity",
        "manifest_identity",
        "resolution",
        "cargo_toolchain_identity",
        "rust_toolchain_identity",
        "native_verifier_identity",
        "execution_roots",
        "cargo_command",
        "rustc_command",
        "cargo_observation",
        "rust_observation",
        "commands",
    }
    if (
        type(oracle) is not RustLibraryAcceptance
        or type(document) is not dict
        or set(document) != fields
    ):
        _refuse("archive_invalid")
    declaration = oracle.rust_harness
    declaration.require_library_alias(library_alias)
    roots = document["execution_roots"]
    if type(roots) is not dict or set(roots) != {"harness", "library", "inputs"}:
        _refuse("archive_roots")
    inputs = roots["inputs"]
    if type(inputs) is not list or len(inputs) != len(declaration.inputs.roots):
        _refuse("archive_roots")
    if any(type(row) is not dict or set(row) != {"scope", "root"} for row in inputs):
        _refuse("archive_roots")
    if tuple(row["scope"] for row in inputs) != tuple(
        name for name, _ in declaration.inputs.roots
    ):
        _refuse("archive_roots")
    coordinates = [roots["harness"], roots["library"], *(row["root"] for row in inputs)]
    if any(
        type(value) is not str or not 0 < len(value) <= 4096 for value in coordinates
    ):
        _refuse("archive_roots")
    path_type = (
        PureWindowsPath if PureWindowsPath(roots["harness"]).drive else PurePosixPath
    )
    paths = [path_type(value) for value in coordinates]
    if any(not value.is_absolute() or ".." in value.parts for value in paths) or any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(paths)
        for right in paths[index + 1 :]
    ):
        _refuse("archive_roots")
    harness, export = paths[:2]
    library = next(
        row
        for row in declaration.resolution.packages
        if row.identity == declaration.resolution.library
    )
    expected_manifest = _manifest(
        oracle,
        library_alias=library_alias,
        library_directory=export.joinpath(
            *PurePosixPath(library.manifest).parts
        ).parent,
    )
    commands = document["commands"]
    phases = ("metadata_before", "build", "metadata_after", "execute")
    command_fields = {
        "phase",
        "argv",
        "environment_identity",
        "exit_code",
        "stdout_identity",
        "stderr_identity",
    }
    if (
        type(commands) is not list
        or len(commands) != 4
        or any(type(row) is not dict or set(row) != command_fields for row in commands)
        or tuple(row["phase"] for row in commands) != phases
    ):
        _refuse("archive_commands")
    cargo, rustc = document["cargo_command"], document["rustc_command"]
    if any(
        type(argv) is not list
        or not 1 <= len(argv) <= 32
        or any(
            type(value) is not str or not 0 < len(value) <= 4096 or "\x00" in value
            for value in argv
        )
        for argv in (cargo, rustc)
    ):
        _refuse("archive_tools")
    cargo_observation, rust_observation = (
        document["cargo_observation"],
        document["rust_observation"],
    )
    cargo_fields = {
        "command",
        "launcher_executable",
        "launcher_digest",
        "version",
        "version_info",
        "identity",
    }
    rust_fields = {
        "command",
        "launcher_executable",
        "launcher_digest",
        "compiler_executable",
        "compiler_digest",
        "version",
        "identity",
    }
    if (
        type(cargo_observation) is not dict
        or set(cargo_observation) != cargo_fields
        or type(rust_observation) is not dict
        or set(rust_observation) != rust_fields
    ):
        _refuse("archive_tools")
    for observation in (cargo_observation, rust_observation):
        if any(
            type(value) is not str or not 0 < len(value) <= 4096
            for name, value in observation.items()
            if name not in {"command", "version_info"}
        ):
            _refuse("archive_tools")
    version_info = cargo_observation["version_info"]
    if (
        type(version_info) is not list
        or len(version_info) != 3
        or any(type(value) is not int or value < 0 for value in version_info)
    ):
        _refuse("archive_tools")
    if (
        cargo_observation["command"] != cargo
        or rust_observation["command"] != rustc
        or len(cargo) != 1
        or len(rustc) != 1
    ):
        _refuse("archive_tools")
    if (
        cargo_identity(
            command=tuple(cargo),
            launcher_executable=cargo_observation["launcher_executable"],
            launcher_digest=cargo_observation["launcher_digest"],
            version=cargo_observation["version"],
            version_info=tuple(version_info),
        )
        != declaration.cargo_toolchain.uri
        or cargo_observation["identity"] != declaration.cargo_toolchain.uri
    ):
        _refuse("archive_tools")
    if (
        _rust_toolchain_identity(
            tuple(rustc),
            rust_observation["launcher_executable"],
            rust_observation["launcher_digest"],
            rust_observation["compiler_executable"],
            rust_observation["compiler_digest"],
            rust_observation["version"],
        )
        != declaration.rust_toolchain.uri
        or rust_observation["identity"] != declaration.rust_toolchain.uri
    ):
        _refuse("archive_tools")
    common = ["--offline", "--locked", "--manifest-path", str(harness / "Cargo.toml")]
    cases = [
        {
            "case_id": row.case_id,
            "capability": row.capability,
            "arguments": row.arguments,
        }
        for row in oracle.cases
    ]
    from literate_ai.contracts.identity import canonical_json_bytes
    from literate_ai.contracts.product_json import product_json_bytes

    executable = (
        harness
        / "target/debug"
        / (
            "litai-library-acceptance.exe"
            if path_type is PureWindowsPath
            else "litai-library-acceptance"
        )
    )
    argv = [
        [*cargo, "metadata", "--format-version", "1", *common],
        [*cargo, "build", "--quiet", *common, "--target-dir", str(harness / "target")],
        [*cargo, "metadata", "--format-version", "1", *common],
        [
            str(executable),
            str(export),
            canonical_json_bytes(surface.to_dict()).decode(),
            product_json_bytes(cases).decode(),
        ],
    ]
    with capture_loaded_rust_library_verifier_inputs(oracle) as current:
        if (
            document["schema"] != "literate-ai/rust-library-harness-execution@1"
            or document["oracle_identity"] != oracle.identity.uri
            or document["declaration_identity"]
            != canonical_identity(declaration.to_dict()).uri
            or document["verifier_inputs_identity"] != current.identity.uri
            or document["input_owners_identity"]
            != declaration.inputs.owners_identity.uri
            or document["lockfile_identity"] != declaration.lockfile.identity.uri
            or document["resolution"] != declaration.resolution.to_dict()
            or document["cargo_toolchain_identity"] != declaration.cargo_toolchain.uri
            or document["rust_toolchain_identity"] != declaration.rust_toolchain.uri
            or reader.read_bytes(declaration.lockfile.identity) != current.lockfile
            or reader.read_bytes(
                ContentIdentity.parse_uri(document["manifest_identity"])
            )
            != expected_manifest
        ):
            _refuse("archive_custody")
        executable_bytes = reader.read_bytes(
            ContentIdentity.parse_uri(document["native_verifier_identity"])
        )
        if not 0 < len(executable_bytes) <= 256 * 1024**2:
            _refuse("archive_output")
        environment = None
        for index, row in enumerate(commands):
            if (
                row["argv"] != argv[index]
                or type(row["exit_code"]) is not int
                or row["exit_code"] != 0
            ):
                _refuse("archive_commands")
            observed_environment = ContentIdentity.parse_uri(
                row["environment_identity"]
            )
            if environment is not None and environment != observed_environment:
                _refuse("archive_environment")
            environment = observed_environment
            stdout = reader.read_bytes(
                ContentIdentity.parse_uri(row["stdout_identity"])
            )
            stderr = reader.read_bytes(
                ContentIdentity.parse_uri(row["stderr_identity"])
            )
            if (
                len(stdout) > declaration.stdout_limit_bytes
                or len(stderr) > declaration.stderr_limit_bytes
            ):
                _refuse("archive_output")
            if index in (0, 2):
                observe_rust_harness_resolution(
                    stdout,
                    declaration=declaration,
                    harness=harness,
                    export=export,
                    roots=tuple(
                        (row["scope"], path_type(row["root"])) for row in inputs
                    ),
                    current_paths=False,
                )
            elif index == 3:
                _require_results(stdout, oracle)
        current.require_current()
