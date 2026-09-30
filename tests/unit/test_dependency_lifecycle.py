from __future__ import annotations

import base64
import copy
import hashlib
import importlib.metadata
import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    DependencyObservationError,
    HostDependencyObservation,
    LinuxElfDependencyObserver,
    MacOsMachODependencyObserver,
    build_cyclonedx_bom,
    declare_optional_python_distributions,
    installed_python_distribution_payload,
    observe_installed_python_distributions,
    parse_dumpbin_dependents,
    parse_dyld_info_links,
    parse_dyld_info_rpaths,
    parse_dyld_info_summary,
    parse_ldconfig_cache,
    parse_llvm_readobj_imports,
    parse_readelf_dynamic,
    reconcile_generated_dependencies,
)
from literate_ai.adapters.dependencies import observation as dependency_lifecycle
from literate_ai.adapters.dependencies.acquisition import _generated_lock_projection
from literate_ai.adapters.dependencies.observation import (
    _ElfImage,
    _file_digest,
    _find_windows_pe_inspector,
    _is_pe,
    _macho_component,
    _macho_ref,
    _native_dependency_edges,
    _npm_launcher_dependencies,
    _npm_launcher_package_root,
    _npm_package_graph,
    _npm_package_own_files,
    _parse_dumpbin_import_records,
    _parse_llvm_readobj_import_records,
    _parse_windows_api_set_namespace,
    _pe_closure,
    _PeInspector,
    _require_no_link_components,
    _resolve_pe_import,
    _stable_file_bytes,
    _windows_npm_cmd_target,
    _WindowsApiSetSchema,
)
from literate_ai.adapters.dependencies.resolution import (
    _bazel_module_intent,
    _require_raw_bazel_graph,
    _require_raw_bazel_lock,
    _require_raw_bazel_repositories,
    _require_version_in_range,
    _resolved_source_projection,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    canonical_identity,
    canonical_json_bytes,
    component_bom_ref,
)
from literate_ai.ports import PortContractError, require_dependency_resolution_result
from literate_ai.ports.contracts import (
    BuildDependencyEvidenceArtifact,
    BuildDependencyObservation,
    BuildInputConsumption,
    BzlmodModule,
    BzlmodRootModule,
)


def _managed_graph() -> CycloneDxManagedGraph:
    identity = canonical_identity({"fixture": "dependency-lifecycle"})
    root_ref = component_bom_ref(identity)
    return CycloneDxManagedGraph(
        root_ref,
        (
            CycloneDxManagedComponent(
                root_ref,
                ManagedComponentKind.ROOT,
                identity,
                "urn:literate-ai:component:fixture/application",
                "1.0.0",
                (),
            ),
        ),
        (),
        canonical_identity({"fixture": "dependency-lifecycle-composition"}),
    )


def _inventory(*names: str) -> bytes:
    return json.dumps(
        {
            "metadata": {"component": {"name": "fixture-app"}},
            "components": [{"name": name} for name in names],
        }
    ).encode()


def _bazel_fixture_source(
    graph: CycloneDxManagedGraph,
    *,
    aggregate: str = "incomplete_third_party_only",
    exact: bool = False,
) -> bytes:
    dependency: dict[str, object] = {
        "type": "library",
        "bom-ref": "pkg:generic/rules_cc",
        "name": "rules_cc",
        "purl": "pkg:generic/rules_cc",
        "isExternal": True,
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "build"},
            {"name": "literate-ai:dependency-scope", "value": "build"},
            {
                "name": "literate-ai:bzlmod-requested-version",
                "value": "0.1.0",
            },
        ],
    }
    if exact:
        dependency["version"] = "0.1.0"
    else:
        dependency["versionRange"] = "vers:generic/>=0.1.0"
    source, _ = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=graph,
        additional_components=(dependency,),
        additional_edges=((graph.root_ref, "pkg:generic/rules_cc"),),
        composition_aggregate=aggregate,
    )
    return source


def _bzlmod_evidence_contents() -> dict[str, bytes]:
    rules_reference = {
        "key": "rules_cc@0.2.0",
        "name": "rules_cc",
        "version": "0.2.0",
        "apparentName": "rules_cc",
        "unexpanded": True,
    }
    platforms = {
        "key": "platforms@0.0.10",
        "name": "platforms",
        "version": "0.0.10",
        "apparentName": "platforms",
        "dependencies": [],
        "indirectDependencies": [],
        "cycles": [rules_reference],
    }
    rules = {
        "key": "rules_cc@0.2.0",
        "name": "rules_cc",
        "version": "0.2.0",
        "apparentName": "rules_cc",
        "dependencies": [platforms],
        "indirectDependencies": [],
        "cycles": [],
    }
    graph = json.dumps(
        {
            "key": "<root>",
            "name": "fixture_app",
            "version": "1.0.0",
            "apparentName": "fixture_app",
            "root": True,
            "dependencies": [rules],
            "indirectDependencies": [],
            "cycles": [],
        },
        indent=2,
    ).encode()
    registry_hashes: dict[str, str] = {}
    for name, version in (("platforms", "0.0.10"), ("rules_cc", "0.2.0")):
        prefix = f"https://bcr.example/modules/{name}/{version}"
        registry_hashes[f"{prefix}/MODULE.bazel"] = "a" * 64
        registry_hashes[f"{prefix}/source.json"] = "b" * 64
    lock = json.dumps(
        {
            "lockFileVersion": 11,
            "registryFileHashes": registry_hashes,
            "selectedYankedVersions": {},
            "moduleExtensions": {"fixture-extension": {"general": {}}},
            "facts": {},
            "factsVersions": {},
        },
        indent=2,
    ).encode()
    integrity = "sha256-" + base64.b64encode(b"i" * 32).decode()
    repository_records = []
    for name, version in (("platforms", "0.0.10"), ("rules_cc", "0.2.0")):
        repository_records.append(
            {
                "canonicalName": name if name == "platforms" else f"{name}+",
                "repoRuleName": "http_archive",
                "repoRuleBzlLabel": "@@bazel_tools//tools/build_defs/repo:http.bzl",
                "moduleKey": f"{name}@{version}",
                "attribute": [
                    {
                        "name": "integrity",
                        "type": "STRING",
                        "stringValue": integrity,
                    },
                    {
                        "name": "remote_module_file_integrity",
                        "type": "STRING",
                        "stringValue": integrity,
                    },
                    {
                        "name": "remote_module_file_urls",
                        "type": "STRING_LIST",
                        "stringListValue": [
                            "https://bcr.bazel.build/modules/"
                            f"{name}/{version}/MODULE.bazel"
                        ],
                    },
                    {
                        "name": "urls",
                        "type": "STRING_LIST",
                        "stringListValue": [
                            f"https://archives.example/{name}-{version}.tar.gz"
                        ],
                    },
                ],
            }
        )
    repositories = b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for record in repository_records
    )
    return {
        ".literate/bazel/MODULE.bazel.lock": lock,
        ".literate/bazel/module-graph.json": graph,
        ".literate/bazel/repositories.ndjson": repositories,
    }


def _fixture_bazel_toolchain() -> dict[str, object]:
    material: dict[str, object] = {
        "command": ["/opt/fixture/bin/bazel"],
        "launcher_digest": "sha256:" + "c" * 64,
        "launcher_executable": "/opt/fixture/bin/bazel",
        "version": "bazel 9.2.0",
    }
    return {**material, "identity": canonical_identity(material).uri}


def _bzlmod_observation(
    *, source_bundle_digest: str, toolchain_identity: str
) -> dict[str, object]:
    contents = _bzlmod_evidence_contents()
    return BuildDependencyObservation(
        resolver_id="bazel/bzlmod@1",
        source_bundle_digest=source_bundle_digest,
        build_toolchain_identity=toolchain_identity,
        resolver_toolchain_identity=str(_fixture_bazel_toolchain()["identity"]),
        root_module=BzlmodRootModule("fixture_app", "1.0.0", ("rules_cc@0.2.0",)),
        modules=(
            BzlmodModule(
                "platforms@0.0.10",
                "platforms",
                "0.0.10",
                ("rules_cc@0.2.0",),
            ),
            BzlmodModule(
                "rules_cc@0.2.0",
                "rules_cc",
                "0.2.0",
                ("platforms@0.0.10",),
            ),
        ),
        evidence_artifacts=(
            BuildDependencyEvidenceArtifact(
                "bazel-module-lock",
                ".literate/bazel/MODULE.bazel.lock",
                "sha256:"
                + hashlib.sha256(
                    contents[".literate/bazel/MODULE.bazel.lock"]
                ).hexdigest(),
            ),
            BuildDependencyEvidenceArtifact(
                "bazel-module-graph",
                ".literate/bazel/module-graph.json",
                "sha256:"
                + hashlib.sha256(
                    contents[".literate/bazel/module-graph.json"]
                ).hexdigest(),
            ),
            BuildDependencyEvidenceArtifact(
                "bazel-repository-definitions",
                ".literate/bazel/repositories.ndjson",
                "sha256:"
                + hashlib.sha256(
                    contents[".literate/bazel/repositories.ndjson"]
                ).hexdigest(),
            ),
        ),
    ).to_dict()


def _write_bzlmod_composite(
    root: Path, *, build: dict[str, object], observation: dict[str, object]
) -> None:
    contents = _bzlmod_evidence_contents()
    consumption = BuildInputConsumption(
        consumer_id="bazel/bzlmod@1",
        source_bundle_digest=str(build["source_bundle_digest"]),
        files=("source/MODULE.bazel", "source/main.cc"),
    )
    contents = {
        **contents,
        ".literate/bazel/buildfiles.txt": b"",
        ".literate/bazel/source-inputs.txt": b"//:main.cc\n",
        ".literate/bazel/build-input-consumption.json": canonical_json_bytes(
            consumption.to_dict()
        ),
    }
    for relative, content in contents.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    files = [
        {
            "path": relative,
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
        for relative, content in sorted(contents.items())
    ]
    manifest = {
        "schema": "urn:literate-ai:schema:v1:bazel-conformance-artifact",
        "authorization_id": build["authorization_id"],
        "builder_id": "builder:fixture@1",
        "delegate_artifact_digest": canonical_identity(
            {"fixture": "delegate-artifact"}
        ).uri,
        "source_bundle_digest": build["source_bundle_digest"],
        "build_toolchain_identity": observation["build_toolchain_identity"],
        "resolver_toolchain_identity": observation["resolver_toolchain_identity"],
        "graph_identity": observation["graph_identity"],
        "evidence_identity": observation["evidence_identity"],
        "observation_identity": observation["observation_identity"],
        "build_input_consumption": consumption.to_dict(),
        "build_input_consumption_identity": consumption.identity,
        "files": files,
    }
    manifest_content = canonical_json_bytes(manifest)
    (root / "build-manifest.json").write_bytes(manifest_content)
    build["artifact_digest"] = "sha256:" + hashlib.sha256(manifest_content).hexdigest()
    build["artifact_path"] = str(root)
    build["build_input_consumption"] = consumption.to_dict()
    build["build_input_consumption_identity"] = consumption.identity


def _npm_cmd_shim(target: str) -> str:
    lines = (
        "@ECHO off",
        "GOTO start",
        ":find_dp0",
        "SET dp0=%~dp0",
        "EXIT /b",
        ":start",
        "SETLOCAL",
        "CALL :find_dp0",
        "",
        'IF EXIST "%dp0%\\node.exe" (',
        '  SET "_prog=%dp0%\\node.exe"',
        ") ELSE (",
        '  SET "_prog=node"',
        "  SET PATHEXT=%PATHEXT:;.JS;=;%",
        ")",
        "",
        "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "
        f'"%_prog%"  "%dp0%\\{target}" %*',
    )
    return "\r\n".join(lines) + "\r\n"


def _minimal_pe(path: Path) -> None:
    content = bytearray(0x84)
    content[:2] = b"MZ"
    content[0x3C:0x40] = (0x80).to_bytes(4, "little")
    content[0x80:0x84] = b"PE\0\0"
    path.write_bytes(content)


def _api_set_namespace_fixture() -> bytes:
    contract = "api-ms-win-core-fixture-l1-1-0".encode("utf-16-le")
    alias = "special.dll".encode("utf-16-le")
    alias_host = "specialhost.dll".encode("utf-16-le")
    default_host = "kernelbase.dll".encode("utf-16-le")
    entry_offset = 28
    value_offset = entry_offset + 24
    string_offset = value_offset + 40
    offsets: dict[str, int] = {}
    payload = bytearray(string_offset)
    for name, value in (
        ("contract", contract),
        ("alias", alias),
        ("alias_host", alias_host),
        ("default_host", default_host),
    ):
        offsets[name] = len(payload)
        payload.extend(value)

    def put_u32(offset: int, value: int) -> None:
        payload[offset : offset + 4] = value.to_bytes(4, "little")

    put_u32(0, 6)
    put_u32(4, len(payload))
    put_u32(12, 1)
    put_u32(16, entry_offset)
    put_u32(entry_offset + 4, offsets["contract"])
    put_u32(entry_offset + 8, len(contract))
    put_u32(entry_offset + 12, len(contract) - len("-0".encode("utf-16-le")))
    put_u32(entry_offset + 16, value_offset)
    put_u32(entry_offset + 20, 2)
    put_u32(value_offset + 4, offsets["alias"])
    put_u32(value_offset + 8, len(alias))
    put_u32(value_offset + 12, offsets["alias_host"])
    put_u32(value_offset + 16, len(alias_host))
    put_u32(value_offset + 20 + 12, offsets["default_host"])
    put_u32(value_offset + 20 + 16, len(default_host))
    return bytes(payload)


class _ObservedRuntime:
    def observe(self, build, *, root_ref: str) -> HostDependencyObservation:
        del build
        runtime_ref = "pkg:generic/observed-runtime@1.2.3"
        return HostDependencyObservation(
            (
                {
                    "type": "library",
                    "bom-ref": runtime_ref,
                    "name": "observed-runtime",
                    "version": "1.2.3",
                    "properties": [
                        {
                            "name": "literate-ai:dependency-kind",
                            "value": "system",
                        },
                        {
                            "name": "literate-ai:dependency-scope",
                            "value": "runtime",
                        },
                    ],
                },
            ),
            ((root_ref, runtime_ref),),
        )


class _EmptyObserver:
    def observe(self, build, *, root_ref: str) -> HostDependencyObservation:
        del build, root_ref
        return HostDependencyObservation((), ())


class _FixtureMacObserver(MacOsMachODependencyObserver):
    def __init__(self, accepted: set[str]) -> None:
        super().__init__(lifecycle_commands=())
        self.accepted = accepted

    def _run_dyld(self, arguments) -> str:
        if (
            len(arguments) == 2
            and arguments[0] == "-validate_only"
            and arguments[-1] in self.accepted
        ):
            return "valid"
        raise DependencyObservationError("fixture.rejected", "image is absent")


class _FixtureDistribution:
    def __init__(
        self,
        root: Path,
        name: str,
        *,
        requires: tuple[str, ...] = (),
    ) -> None:
        self.root = root
        self.metadata = {"Name": name}
        self.version = "1.0.0"
        self.requires = requires
        self.files = (Path(f"{name}.py"),)
        (root / f"{name}.py").write_text(f"NAME = {name!r}\n")

    def locate_file(self, entry) -> Path:
        return self.root / Path(entry)


class _RecordedDistribution:
    def __init__(
        self,
        root: Path,
        name: str,
        files: tuple[PurePosixPath, ...],
    ) -> None:
        self.root = root
        self.metadata = {"Name": name}
        self.version = "1.0.0"
        self.requires: tuple[str, ...] = ()
        self.files = files

    def locate_file(self, entry) -> Path:
        return self.root.joinpath(*PurePosixPath(str(entry)).parts)


class DependencyLifecycleTests(unittest.TestCase):
    def test_declared_optional_python_import_is_source_authority_not_installation(self):
        root_ref = "urn:fixture:python-root"
        observation = declare_optional_python_distributions(
            {
                "browser": ("playwright==1.62.0",),
                "dev": ("pytest-xdist>=3.6",),
            },
            imported_names=("playwright",),
            root_ref=root_ref,
        )

        self.assertEqual(len(observation.components), 1)
        component = observation.components[0]
        self.assertEqual(component["bom-ref"], "pkg:pypi/playwright")
        self.assertEqual(component["version"], "1.62.0")
        self.assertEqual(component["scope"], "optional")
        self.assertEqual(observation.edges, ((root_ref, "pkg:pypi/playwright"),))
        properties = component["properties"]
        self.assertIn(
            {
                "name": "literate-ai:python-optional-requirement",
                "value": '{"group":"browser","requirement":"playwright==1.62.0"}',
            },
            properties,
        )

    def test_optional_distribution_alias_comes_from_exact_installed_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "grpc" / "__init__.py"
            source.parent.mkdir()
            source.write_text("VALUE = 1\n", encoding="utf-8")
            distribution = _RecordedDistribution(
                root, "grpcio", (PurePosixPath("grpc/__init__.py"),)
            )
            with mock.patch(
                "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
                return_value=distribution,
            ):
                observation = declare_optional_python_distributions(
                    {"grpc": ("grpcio==1.0.0",)},
                    imported_names=("grpc",),
                    root_ref="urn:fixture:python-root",
                )

        self.assertEqual(len(observation.components), 1)
        component = observation.components[0]
        self.assertEqual(component["name"], "grpcio")
        self.assertIn(
            {"name": "literate-ai:python-top-level-import", "value": "grpc"},
            component["properties"],
        )
        with mock.patch(
            "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
            side_effect=importlib.metadata.PackageNotFoundError,
        ):
            replayed = declare_optional_python_distributions(
                {"grpc": ("grpcio==1.0.0",)},
                imported_names=("grpc",),
                root_ref="urn:fixture:python-root",
                import_names_by_distribution={"grpcio": ("grpc",)},
            )
        self.assertEqual(replayed.components, observation.components)

    def test_imported_optional_python_requirement_must_be_exact(self):
        with self.assertRaises(DependencyObservationError) as caught:
            declare_optional_python_distributions(
                {"browser": ("playwright>=1.62",)},
                imported_names=("playwright",),
                root_ref="urn:fixture:python-root",
            )

        self.assertEqual(
            caught.exception.code,
            "dependencies.python-optional-requirement-unpinned",
        )

    def test_opaque_npm_lifecycle_launcher_is_not_an_execution_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "node_modules" / "@bazel" / "bazelisk"
            package.mkdir(parents=True)
            launcher = package / "bazelisk.js"
            launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@bazel/bazelisk",
                        "version": "1.28.1",
                        "bin": {"bazel": "bazelisk.js"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DependencyObservationError) as raised:
                _npm_launcher_dependencies(
                    launcher,
                    "bazel",
                    root_ref="urn:fixture:root",
                    prefix="toolchain-launcher",
                    native_predicate=lambda _path: True,
                )
            self.assertEqual(
                raised.exception.code,
                "dependencies.lifecycle-launcher-unsupported",
            )

    def test_bazelisk_npm_toolchain_binds_package_and_node_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            node_modules = Path(temporary) / "node_modules"
            package = node_modules / "@bazel" / "bazelisk"
            package.mkdir(parents=True)
            launcher = package / "bazelisk.js"
            launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@bazel/bazelisk",
                        "version": "v1.28.1",
                        "bin": {"bazel": "bazelisk.js"},
                    }
                ),
                encoding="utf-8",
            )
            node = Path(temporary) / ("node.exe" if os.name == "nt" else "node")
            node.write_bytes(b"native-node")

            with mock.patch(
                "literate_ai.adapters.dependencies.observation.shutil.which",
                return_value=str(node),
            ):
                observed = dependency_lifecycle._npm_toolchain_launcher_dependencies(
                    launcher.resolve(),
                    "bazel",
                    root_ref="urn:fixture:root",
                    native_predicate=lambda path: path == node.resolve(),
                )

            self.assertIsNotNone(observed)
            assert observed is not None
            graph, owners, runtime_edges = observed
            package_ref = next(
                str(component["bom-ref"])
                for component in graph.components
                if component.get("name") == "@bazel/bazelisk"
            )
            package_component = next(
                component
                for component in graph.components
                if component.get("name") == "@bazel/bazelisk"
            )
            self.assertEqual(package_component["version"], "v1.28.1")
            self.assertIn(("urn:fixture:root", package_ref), graph.edges)
            self.assertEqual(owners, {})
            self.assertEqual(runtime_edges, ((package_ref, str(node.resolve())),))

            with mock.patch(
                "literate_ai.adapters.dependencies.observation.shutil.which",
                return_value=str(node),
            ):
                resolved_target_observation = (
                    dependency_lifecycle._npm_toolchain_launcher_dependencies(
                        launcher.resolve(),
                        str(launcher.resolve()),
                        root_ref="urn:fixture:root",
                        native_predicate=lambda path: path == node.resolve(),
                    )
                )
            self.assertIsNotNone(resolved_target_observation)

    def test_bazelisk_windows_npm_shim_binds_package_and_node_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            package = prefix / "node_modules" / "@bazel" / "bazelisk"
            package.mkdir(parents=True)
            target = package / "bazelisk.js"
            target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@bazel/bazelisk",
                        "version": "1.28.1",
                        "bin": {"bazel": "bazelisk.js"},
                    }
                ),
                encoding="utf-8",
            )
            launcher = prefix / "bazel.cmd"
            launcher.write_bytes(
                _npm_cmd_shim("node_modules\\@bazel\\bazelisk\\bazelisk.js").encode()
            )
            node = prefix / "node.exe"
            node.write_bytes(b"native-node")

            with mock.patch(
                "literate_ai.adapters.dependencies.observation.shutil.which",
                return_value=str(node),
            ):
                observed = dependency_lifecycle._npm_toolchain_launcher_dependencies(
                    launcher,
                    "bazel",
                    root_ref="urn:fixture:root",
                    native_predicate=lambda path: path == node.resolve(),
                )

            self.assertIsNotNone(observed)
            assert observed is not None
            graph, owners, runtime_edges = observed
            package_ref = next(
                str(component["bom-ref"])
                for component in graph.components
                if component.get("name") == "@bazel/bazelisk"
            )
            self.assertIn(("urn:fixture:root", package_ref), graph.edges)
            self.assertEqual(owners, {})
            self.assertEqual(runtime_edges, ((package_ref, str(node.resolve())),))

    def test_npm_graph_enforces_one_aggregate_file_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps({"name": "fixture", "version": "1.0.0"}),
                encoding="utf-8",
            )
            (root / "payload.js").write_text("fixture\n", encoding="utf-8")
            with (
                mock.patch(
                    "literate_ai.adapters.dependencies.observation._MAX_NPM_GRAPH_FILES",
                    1,
                ),
                self.assertRaises(DependencyObservationError) as raised,
            ):
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                raised.exception.code, "dependencies.npm-graph-budget-exceeded"
            )

    def test_npm_graph_rejects_noncanonical_prefixed_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps({"name": "fixture", "version": "vv1.28.1"}),
                encoding="utf-8",
            )

            with self.assertRaises(DependencyObservationError) as raised:
                _npm_package_graph(root, native_predicate=lambda _path: False)

            self.assertEqual(raised.exception.code, "dependencies.npm-version-invalid")

    def test_npm_graph_bounds_manifest_bytes_before_retaining_the_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dependency = root / "node_modules" / "dependency"
            dependency.mkdir(parents=True)
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "root",
                        "version": "1.0.0",
                        "dependencies": {"dependency": "1.0.0"},
                    }
                ),
                encoding="utf-8",
            )
            (dependency / "package.json").write_text(
                json.dumps({"name": "dependency", "version": "1.0.0"}),
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "literate_ai.adapters.dependencies.observation._MAX_NPM_GRAPH_MANIFEST_BYTES",
                    1,
                ),
                self.assertRaises(DependencyObservationError) as raised,
            ):
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                raised.exception.code, "dependencies.npm-graph-budget-exceeded"
            )

    def test_native_inspection_seeds_do_not_flatten_transitive_sbom_edges(self) -> None:
        edges = _native_dependency_edges(
            "urn:fixture:root",
            refs={
                "/artifact": "urn:native:artifact",
                "/artifact-lib": "urn:native:artifact-lib",
                "/host-node": "urn:native:host-node",
                "/bundle-node": "urn:native:bundle-node",
            },
            direct_seed_paths={"/artifact"},
            image_edges=(("/artifact", "/artifact-lib"),),
            native_owners={"/bundle-node": "urn:npm:bundle"},
            launcher_runtime_edges=(("urn:launcher:host", "/host-node"),),
        )
        self.assertIn(("urn:fixture:root", "urn:native:artifact"), edges)
        self.assertIn(("urn:native:artifact", "urn:native:artifact-lib"), edges)
        self.assertIn(("urn:npm:bundle", "urn:native:bundle-node"), edges)
        self.assertIn(("urn:launcher:host", "urn:native:host-node"), edges)
        self.assertNotIn(("urn:fixture:root", "urn:native:host-node"), edges)
        self.assertNotIn(("urn:fixture:root", "urn:native:bundle-node"), edges)

    def test_installed_import_aliases_come_from_verified_payload_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contents = {
                "jwt/__init__.py": b"VALUE = 1\n",
                "namespace/child/module.py": b"VALUE = 2\n",
                "extension.abi3.so": b"extension fixture",
                "standalone.py": b"VALUE = 3\n",
                "stubs/only.pyi": b"VALUE: int\n",
                "data/only.json": b"{}",
                "fixture-1.0.0.dist-info/top_level.txt": b"invented_alias\n",
            }
            for relative, content in contents.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            distribution = _RecordedDistribution(
                root, "fixture", tuple(PurePosixPath(p) for p in contents)
            )
            with mock.patch(
                "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
                return_value=distribution,
            ):
                observation = observe_installed_python_distributions(
                    ("fixture==1.0.0",), root_ref="urn:fixture:root"
                )
            properties = observation.components[0]["properties"]
            aliases = {
                p["value"]
                for p in properties
                if p["name"] == "literate-ai:python-top-level-import"
            }
            self.assertEqual(aliases, {"jwt", "namespace", "extension", "standalone"})
            bom = json.dumps({"components": observation.components}).encode()
            reconcile_generated_dependencies({"app.py": "import jwt\n"}, bom)
            with self.assertRaisesRegex(DependencyObservationError, "invented.alias"):
                reconcile_generated_dependencies(
                    {"app.py": "import invented_alias\n"}, bom
                )

    def test_python_distribution_observation_includes_transitive_file_closure(
        self,
    ) -> None:
        root_ref = "urn:fixture:python-root"
        observation = observe_installed_python_distributions(
            (
                "cyclonedx-python-lib[json-validation]==11.11.0",
                "univers==32.0.1",
            ),
            root_ref=root_ref,
        )
        inventory = {item["bom-ref"]: item for item in observation.components}

        self.assertIn("pkg:pypi/univers", inventory)
        self.assertIn("pkg:pypi/packaging", inventory)
        self.assertIn("pkg:pypi/jsonschema", inventory)
        self.assertIn("pkg:pypi/fqdn", inventory)
        self.assertIn((root_ref, "pkg:pypi/univers"), observation.edges)
        self.assertIn(
            ("pkg:pypi/cyclonedx-python-lib", "pkg:pypi/jsonschema"),
            observation.edges,
        )
        self.assertIn(("pkg:pypi/jsonschema", "pkg:pypi/fqdn"), observation.edges)
        self.assertIn(("pkg:pypi/univers", "pkg:pypi/packaging"), observation.edges)
        self.assertRegex(
            inventory["pkg:pypi/univers"]["hashes"][0]["content"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            observe_installed_python_distributions(
                ("packaging; python_version < '0'",), root_ref=root_ref
            ).components,
            (),
        )

    def test_python_marker_context_uses_only_requested_extras(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            distributions = {
                "root": _FixtureDistribution(
                    root,
                    "root",
                    requires=(
                        'included==1.0.0; extra == "foo"',
                        'excluded==1.0.0; extra != "foo"',
                    ),
                ),
                "included": _FixtureDistribution(root, "included"),
                "excluded": _FixtureDistribution(root, "excluded"),
            }
            with mock.patch(
                "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
                side_effect=lambda name: distributions[str(name)],
            ):
                with_extra = observe_installed_python_distributions(
                    ("root[foo]==1.0.0",), root_ref="urn:fixture:root"
                )
                without_extra = observe_installed_python_distributions(
                    ("root==1.0.0",), root_ref="urn:fixture:root"
                )

        with_extra_names = {item["name"] for item in with_extra.components}
        without_extra_names = {item["name"] for item in without_extra.components}
        self.assertEqual(with_extra_names, {"root", "included"})
        self.assertEqual(without_extra_names, {"root", "excluded"})

    def test_self_host_dependency_projection_is_exact_and_declared_only(self) -> None:
        graph = _managed_graph()
        dependencies = observe_installed_python_distributions(
            ("univers==32.0.1",), root_ref=graph.root_ref
        )
        content, _binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=dependencies.components,
            additional_edges=dependencies.edges,
        )
        harness = runpy.run_path(
            str(
                Path(__file__).resolve().parents[2]
                / "tests/conformance/support/self_hosting_proof.py"
            )
        )
        project = harness["_candidate_dependency_projection"]
        error = harness["SelfHostSampleError"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            bom = candidate / ".literate" / "sbom.cdx.json"
            bom.parent.mkdir(parents=True)
            bom.write_bytes(content)
            projection = project(candidate, root / "projection")
            self.assertTrue((projection / "univers").is_dir())
            self.assertFalse((projection / "cyclonedx").exists())
            projected_distributions = {
                str(item.metadata["Name"]).lower().replace("_", "-"): item
                for item in importlib.metadata.distributions(path=[str(projection)])
            }

            def from_projection(name: str) -> importlib.metadata.Distribution:
                normalized = name.lower().replace("_", "-")
                try:
                    return projected_distributions[normalized]
                except KeyError as exc:
                    raise importlib.metadata.PackageNotFoundError(name) from exc

            with mock.patch(
                "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
                side_effect=from_projection,
            ):
                projected_observation = observe_installed_python_distributions(
                    ("univers==32.0.1",), root_ref=graph.root_ref
                )
            self.assertEqual(projected_observation, dependencies)

            document = json.loads(content)
            document["components"] = [
                item
                for item in document["components"]
                if item.get("bom-ref") != "pkg:pypi/packaging"
            ]
            document["dependencies"] = [
                {
                    **item,
                    "dependsOn": [
                        target
                        for target in item["dependsOn"]
                        if target != "pkg:pypi/packaging"
                    ],
                }
                for item in document["dependencies"]
                if item.get("ref") != "pkg:pypi/packaging"
            ]
            bom.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
            with self.assertRaises(error):
                project(candidate, root / "omitted-projection")

            bom.write_bytes(content)
            document = json.loads(content)
            for item in document["dependencies"]:
                if item.get("ref") == "pkg:pypi/univers":
                    item["dependsOn"] = [
                        target
                        for target in item["dependsOn"]
                        if target != "pkg:pypi/packaging"
                    ]
            bom.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
            with self.assertRaises(error):
                project(candidate, root / "edge-projection")

    def test_self_host_projection_ignores_external_derived_bytecode_only(
        self,
    ) -> None:
        graph = _managed_graph()
        harness = runpy.run_path(
            str(
                Path(__file__).resolve().parents[2]
                / "tests/conformance/support/self_hosting_proof.py"
            )
        )
        project = harness["_candidate_dependency_projection"]
        error = harness["SelfHostSampleError"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "environment" / "lib" / "python" / "site-packages"
            installed.mkdir(parents=True)
            source_entry = PurePosixPath("fixture_dependency.py")
            metadata_entry = PurePosixPath(
                "fixture_dependency-1.0.0.dist-info/METADATA"
            )
            record_entry = PurePosixPath("fixture_dependency-1.0.0.dist-info/RECORD")
            bytecode_entry = PurePosixPath(
                "../../../../external-pycache/fixture_dependency.cpython-312.pyc"
            )
            for entry, content in (
                (source_entry, b"VALUE = 1\n"),
                (metadata_entry, b"Name: fixture-dependency\nVersion: 1.0.0\n"),
                (
                    record_entry,
                    f"{source_entry},,\n{bytecode_entry},,\n".encode(),
                ),
                (bytecode_entry, b"derived-bytecode-one"),
            ):
                path = installed.joinpath(*entry.parts).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            distribution = _RecordedDistribution(
                installed,
                "fixture-dependency",
                (source_entry, metadata_entry, record_entry, bytecode_entry),
            )

            with mock.patch(
                "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
                return_value=distribution,
            ):
                first = observe_installed_python_distributions(
                    ("fixture-dependency==1.0.0",), root_ref=graph.root_ref
                )
                installed.joinpath(*bytecode_entry.parts).resolve().write_bytes(
                    b"derived-bytecode-two"
                )
                second = observe_installed_python_distributions(
                    ("fixture-dependency==1.0.0",), root_ref=graph.root_ref
                )
            self.assertEqual(first, second)
            self.assertEqual(
                tuple(
                    item[0]
                    for item in installed_python_distribution_payload(distribution)
                ),
                (metadata_entry, record_entry, source_entry),
            )

            content, _binding = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=graph,
                additional_components=first.components,
                additional_edges=first.edges,
            )
            candidate = root / "candidate"
            bom = candidate / ".literate" / "sbom.cdx.json"
            bom.parent.mkdir(parents=True)
            bom.write_bytes(content)
            projection_root = root / "projection"
            with mock.patch(
                "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
                return_value=distribution,
            ):
                projection = project(candidate, projection_root)

            self.assertTrue((projection / source_entry).is_file())
            self.assertFalse(any(projection_root.rglob("*.pyc")))

            escaping_entry = PurePosixPath("../../../../unrelated/orphan.pyc")
            escaping_path = installed.joinpath(*escaping_entry.parts).resolve()
            escaping_path.parent.mkdir(parents=True, exist_ok=True)
            escaping_path.write_bytes(b"not derived from recorded source")
            escaping_distribution = _RecordedDistribution(
                installed,
                "fixture-dependency",
                (
                    source_entry,
                    metadata_entry,
                    record_entry,
                    escaping_entry,
                ),
            )
            with mock.patch(
                "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
                return_value=escaping_distribution,
            ):
                escaping = observe_installed_python_distributions(
                    ("fixture-dependency==1.0.0",), root_ref=graph.root_ref
                )
            escaping_content, _binding = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=graph,
                additional_components=escaping.components,
                additional_edges=escaping.edges,
            )
            bom.write_bytes(escaping_content)
            with (
                mock.patch(
                    "literate_ai.adapters.dependencies.observation.importlib.metadata.distribution",
                    return_value=escaping_distribution,
                ),
                self.assertRaisesRegex(error, "escaped its root"),
            ):
                project(candidate, root / "escaping-projection")

    def test_npm_observation_excludes_orphans_and_self_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def manifest(path: Path, value: dict[str, object]) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "package.json").write_text(json.dumps(value))

            manifest(
                root,
                {
                    "name": "launcher",
                    "version": "1.0.0",
                    "dependencies": {"child": "^1.0.0"},
                    "optionalDependencies": {"missing-optional": "^1.0.0"},
                    "peerDependencies": {
                        "peer": "~2.1.0",
                        "missing-peer": "^3.0.0",
                    },
                    "peerDependenciesMeta": {"missing-peer": {"optional": True}},
                },
            )
            manifest(
                root / "node_modules" / "child",
                {
                    "name": "child",
                    "version": "1.0.0",
                    "dependencies": {"child": "^1.0.0"},
                },
            )
            manifest(
                root / "node_modules" / "peer",
                {"name": "peer", "version": "2.1.5"},
            )
            orphan = root / "node_modules" / "orphan"
            orphan.mkdir(parents=True)
            (orphan / "package.json").write_text("{not-json")

            observation, _native_owners, launcher_ref = _npm_package_graph(
                root, native_predicate=lambda _path: False
            )

            launcher = next(
                item for item in observation.components if item["name"] == "launcher"
            )
            edge_evidence = {
                tuple(sorted(json.loads(str(item["value"])).items()))
                for item in launcher["properties"]
                if item["name"] == "literate-ai:npm-dependency-edge"
            }

            manifest(
                root,
                {
                    "name": "launcher",
                    "version": "1.0.0",
                    "dependencies": {"child": "^9.0.0"},
                },
            )
            with self.assertRaises(DependencyObservationError) as mismatch:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                mismatch.exception.code, "dependencies.npm-selector-mismatch"
            )

            manifest(
                root,
                {
                    "name": "launcher",
                    "version": "1.0.0",
                    "dependencies": {"child": "file:../child"},
                },
            )
            with self.assertRaises(DependencyObservationError) as unsupported:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                unsupported.exception.code, "dependencies.npm-selector-unsupported"
            )

            manifest(
                root,
                {
                    "name": "launcher",
                    "version": "1.0.0",
                    "peerDependencies": {"required-peer": "^1.0.0"},
                },
            )
            with self.assertRaises(DependencyObservationError) as missing:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                missing.exception.code, "dependencies.npm-dependency-missing"
            )

            manifest(
                root / "node_modules" / "orphan",
                {"name": "orphan", "version": "1.0.0"},
            )

        names = {str(item["name"]) for item in observation.components}
        self.assertEqual(names, {"launcher", "child", "peer"})
        self.assertTrue(any(source == launcher_ref for source, _ in observation.edges))
        self.assertFalse(any(source == target for source, target in observation.edges))
        evidence_values = [dict(item) for item in edge_evidence]
        self.assertTrue(
            any(
                item["kind"] == "peer" and item["optional"] is False
                for item in evidence_values
            )
        )
        self.assertTrue(
            any(
                item["name"] == "missing-peer"
                and item["optional"] is True
                and item["target_ref"] is None
                for item in evidence_values
            )
        )

    def test_npm_observation_accepts_alias_whitespace_and_hashed_hoisting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def manifest(path: Path, value: dict[str, object]) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "package.json").write_text(json.dumps(value), encoding="utf-8")

            manifest(
                root,
                {
                    "name": "npm",
                    "version": "10.0.0",
                    "main": "index.js",
                    "dependencies": {
                        "config": "1.0.0",
                        "node-gyp": ">= 2.1.2 < 3.0.0",
                        "pretty": "npm:@scope/pretty",
                        "string-width-cjs": "npm:string-width@^4.2.0",
                    },
                },
            )
            (root / "index.js").write_text(
                "require('config')\nrequire('pretty')\nrequire('string-width-cjs')\n",
                encoding="utf-8",
            )
            config = root / "node_modules" / "config"
            manifest(config, {"name": "config", "version": "1.0.0"})
            (config / "index.js").write_text("require('node-gyp')\n", encoding="utf-8")
            manifest(
                root / "node_modules" / "node-gyp",
                {"name": "node-gyp", "version": "2.1.2"},
            )
            manifest(
                root / "node_modules" / "string-width-cjs",
                {"name": "string-width", "version": "4.2.3"},
            )
            manifest(
                root / "node_modules" / "pretty",
                {"name": "@scope/pretty", "version": "1.2.3"},
            )

            observation, _native_owners, _root_ref = _npm_package_graph(
                root, native_predicate=lambda _path: False
            )

        self.assertEqual(
            {component["name"] for component in observation.components},
            {"@scope/pretty", "config", "node-gyp", "npm", "string-width"},
        )

    def test_npm_observation_rejects_invalid_hoist_despite_workspace_copy(
        self,
    ) -> None:
        """Model npm 12.0.0's published Arborist selector mismatch."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def manifest(path: Path, value: dict[str, object]) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "package.json").write_text(json.dumps(value), encoding="utf-8")

            manifest(
                root,
                {
                    "name": "npm",
                    "version": "12.0.0",
                    "dependencies": {
                        "@npmcli/arborist": "10.0.0",
                        "validate-npm-package-name": "8.0.0",
                    },
                    "workspaces": ["workspaces/*"],
                },
            )
            manifest(
                root / "node_modules" / "@npmcli" / "arborist",
                {
                    "name": "@npmcli/arborist",
                    "version": "10.0.0",
                    "dependencies": {"validate-npm-package-name": "^7.0.2"},
                },
            )
            manifest(
                root / "node_modules" / "validate-npm-package-name",
                {"name": "validate-npm-package-name", "version": "8.0.0"},
            )
            manifest(
                root
                / "workspaces"
                / "arborist"
                / "node_modules"
                / "validate-npm-package-name",
                {"name": "validate-npm-package-name", "version": "7.0.2"},
            )

            with self.assertRaises(DependencyObservationError) as rejected:
                _npm_package_graph(root, native_predicate=lambda _path: False)

        self.assertEqual(rejected.exception.code, "dependencies.npm-selector-mismatch")
        self.assertIn("at 8.0.0", str(rejected.exception))
        self.assertIn("'^7.0.2'", str(rejected.exception))

    def test_npm_observation_rejects_name_escape_links_and_manifest_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            boundary = Path(temporary)
            root = boundary / "root"
            outside = boundary / "outside"

            def manifest(path: Path, value: dict[str, object]) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "package.json").write_text(json.dumps(value), encoding="utf-8")

            manifest(outside, {"name": "outside", "version": "1.0.0"})
            manifest(
                root,
                {
                    "name": "launcher",
                    "version": "1.0.0",
                    "dependencies": {"../../../outside": "1.0.0"},
                },
            )
            with self.assertRaises(DependencyObservationError) as traversal:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                traversal.exception.code, "dependencies.npm-declaration-invalid"
            )

            manifest(
                root,
                {
                    "name": "launcher",
                    "version": "1.0.0",
                    "dependencies": {"child": "1.0.0"},
                },
            )
            child = root / "node_modules" / "child"
            child.parent.mkdir(parents=True, exist_ok=True)
            try:
                child.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("host cannot create an adversarial directory link")
            with self.assertRaises(DependencyObservationError) as linked:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                linked.exception.code, "dependencies.npm-dependency-path-unsafe"
            )
            child.unlink()

            manifest(child, {"name": "different-child", "version": "1.0.0"})
            with self.assertRaises(DependencyObservationError) as alias:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                alias.exception.code, "dependencies.npm-dependency-name-mismatch"
            )

    def test_windows_npm_shim_binds_one_exact_scoped_package_without_inventory_walk(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary) / "nonstandard-prefix"
            package = prefix / "node_modules" / "@fixture" / "fixturecli"
            package.mkdir(parents=True)
            target = package / "npm-shim.js"
            target.write_text("// fixture\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@fixture/fixturecli",
                        "version": "1.0.0",
                        "bin": {"fixturecli": "npm-shim.js"},
                    }
                ),
                encoding="utf-8",
            )
            unrelated = prefix / "node_modules" / "unrelated"
            unrelated.mkdir()
            (unrelated / "package.json").write_text("{malformed", encoding="utf-8")
            launcher = prefix / "fixturecli.cmd"
            launcher.write_bytes(
                _npm_cmd_shim(
                    "node_modules\\@fixture\\fixturecli\\npm-shim.js"
                ).encode()
            )

            with mock.patch.object(
                Path,
                "iterdir",
                side_effect=AssertionError("npm prefix inventory must not be walked"),
            ):
                observed = _npm_launcher_package_root(launcher, "fixturecli")

            self.assertEqual(observed, package.resolve())
            other_target = package / "other.js"
            other_target.write_text("// unrelated target\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@fixture/fixturecli",
                        "version": "1.0.0",
                        "bin": {"fixturecli": "other.js"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DependencyObservationError) as mismatch:
                _npm_launcher_package_root(launcher, "fixturecli")
            self.assertEqual(
                mismatch.exception.code,
                "dependencies.npm-launcher-binding-invalid",
            )

    def test_windows_local_npm_bin_shim_binds_its_sibling_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            modules = Path(temporary) / "node_modules"
            package = modules / "@fixture" / "fixturecli"
            package.mkdir(parents=True)
            target = package / "npm-shim.js"
            target.write_text("// fixture\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@fixture/fixturecli",
                        "version": "1.0.0",
                        "bin": {"fixturecli": "npm-shim.js"},
                    }
                ),
                encoding="utf-8",
            )
            bin_root = modules / ".bin"
            bin_root.mkdir()
            launcher = bin_root / "fixturecli.cmd"
            launcher.write_bytes(
                _npm_cmd_shim("..\\@fixture\\fixturecli\\npm-shim.js").encode()
            )

            self.assertEqual(
                _windows_npm_cmd_target(launcher),
                target.resolve(),
            )
            self.assertEqual(
                _npm_launcher_package_root(launcher, "fixturecli"),
                package.resolve(),
            )

    def test_windows_npm_shim_rejects_noncanonical_or_additional_commands(
        self,
    ) -> None:
        valid_target = "node_modules\\@fixture\\fixturecli\\npm-shim.js"
        valid = _npm_cmd_shim(valid_target).encode()
        cases = {
            "additional-command": valid + b"calc.exe\r\n",
            "second-dispatch": valid + valid.splitlines(keepends=True)[-1],
            "variable": _npm_cmd_shim(
                "node_modules\\@fixture\\fixturecli\\%TARGET%"
            ).encode(),
            "delayed-expansion": _npm_cmd_shim(
                "node_modules\\@fixture\\fixturecli\\!TARGET!"
            ).encode(),
            "command-metacharacter": _npm_cmd_shim(
                "node_modules\\@fixture\\fixturecli\\npm&shim.js"
            ).encode(),
            "traversal": _npm_cmd_shim(
                "node_modules\\@fixture\\fixturecli\\..\\evil.js"
            ).encode(),
            "dot-segment": _npm_cmd_shim(
                "node_modules\\.\\@fixture\\fixturecli\\npm-shim.js"
            ).encode(),
            "repeated-separator": _npm_cmd_shim(
                "node_modules\\\\@fixture\\fixturecli\\npm-shim.js"
            ).encode(),
            "alternate-data-stream": _npm_cmd_shim(
                "node_modules\\@fixture\\fixturecli\\npm-shim.js:evil"
            ).encode(),
            "nul": valid.replace(b"node_modules", b"node_modules\x00", 1),
            "invalid-utf8": valid.replace(b"node_modules", b"node_modules\xff", 1),
            "too-many-lines": b"\r\n" * 65,
            "line-too-long": b"x" * (4 * 1024 + 1),
            "oversized": b"x" * (64 * 1024 + 1),
        }
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "fixturecli.cmd"
            for name, content in cases.items():
                with self.subTest(name=name):
                    launcher.write_bytes(content)
                    with self.assertRaises(DependencyObservationError) as rejected:
                        _windows_npm_cmd_target(launcher)
                    self.assertEqual(
                        rejected.exception.code,
                        (
                            "dependencies.npm-launcher-oversized"
                            if name == "oversized"
                            else "dependencies.npm-launcher-invalid"
                        ),
                    )

    def test_npm_direct_launcher_requires_exact_package_name_and_bin_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "node_modules" / "fixturecli"
            package.mkdir(parents=True)
            launcher = package / "cli.js"
            launcher.write_text("// exact target\n", encoding="utf-8")
            other = package / "other.js"
            other.write_text("// another target\n", encoding="utf-8")
            manifest = package / "package.json"
            cases = (
                ("no-bin", {"name": "fixturecli", "version": "1.0.0"}),
                (
                    "other-bin-target",
                    {
                        "name": "fixturecli",
                        "version": "1.0.0",
                        "bin": {"fixturecli": "other.js"},
                    },
                ),
                (
                    "unrelated-command",
                    {
                        "name": "fixturecli",
                        "version": "1.0.0",
                        "bin": {"another-command": "cli.js"},
                    },
                ),
                (
                    "wrong-package-name",
                    {
                        "name": "another-package",
                        "version": "1.0.0",
                        "bin": {"fixturecli": "cli.js"},
                    },
                ),
            )
            for name, value in cases:
                with self.subTest(name=name):
                    manifest.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaises(DependencyObservationError):
                        _npm_launcher_package_root(launcher, "fixturecli")

            manifest.write_text(
                json.dumps(
                    {
                        "name": "fixturecli",
                        "version": "1.0.0",
                        "bin": {"fixturecli": "cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                _npm_launcher_package_root(launcher, "fixturecli"), package.resolve()
            )

            manifest.write_text("{malformed", encoding="utf-8")
            with self.assertRaises(DependencyObservationError) as malformed:
                _npm_launcher_package_root(launcher, "fixturecli")
            self.assertEqual(
                malformed.exception.code,
                "dependencies.npm-launcher-manifest-invalid",
            )
            manifest.write_bytes(b" " * (1024 * 1024 + 1))
            with self.assertRaises(DependencyObservationError) as oversized:
                _npm_launcher_package_root(launcher, "fixturecli")
            self.assertEqual(
                oversized.exception.code,
                "dependencies.npm-launcher-manifest-oversized",
            )

    def test_npm_bin_target_rejects_noncanonical_and_linked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "node_modules" / "fixturecli"
            package.mkdir(parents=True)
            launcher = package / "cli.js"
            launcher.write_text("// exact target\n", encoding="utf-8")
            manifest = package / "package.json"
            unsafe_targets = (
                "../cli.js",
                "./cli.js",
                "bin//cli.js",
                "bin/../cli.js",
                "bin\\cli.js",
                "C:/cli.js",
                "cli.js:stream",
            )
            for target in unsafe_targets:
                with self.subTest(target=target):
                    manifest.write_text(
                        json.dumps(
                            {
                                "name": "fixturecli",
                                "version": "1.0.0",
                                "bin": {"fixturecli": target},
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(DependencyObservationError):
                        _npm_launcher_package_root(launcher, "fixturecli")

            real_directory = package / "real-bin"
            real_directory.mkdir()
            real_target = real_directory / "cli.js"
            real_target.write_text("// linked target\n", encoding="utf-8")
            linked_directory = package / "linked-bin"
            linked_directory.symlink_to(real_directory, target_is_directory=True)
            manifest.write_text(
                json.dumps(
                    {
                        "name": "fixturecli",
                        "version": "1.0.0",
                        "bin": {"fixturecli": "linked-bin/cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DependencyObservationError):
                _npm_launcher_package_root(real_target, "fixturecli")

            outside_manifest = Path(temporary) / "outside-package.json"
            outside_manifest.write_text(
                json.dumps(
                    {
                        "name": "fixturecli",
                        "version": "1.0.0",
                        "bin": {"fixturecli": "cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            manifest.unlink()
            manifest.symlink_to(outside_manifest)
            with self.assertRaises(DependencyObservationError):
                _npm_launcher_package_root(launcher, "fixturecli")

    def test_npm_path_components_reject_reparse_points_and_lstat_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            boundary = Path(temporary).resolve()
            target = boundary / "target.js"
            target.write_text("// fixture\n", encoding="utf-8")
            metadata = target.lstat()
            reparse_metadata = SimpleNamespace(
                st_mode=metadata.st_mode,
                st_file_attributes=0x0400,
            )
            with mock.patch.object(Path, "lstat", return_value=reparse_metadata):
                with self.assertRaises(DependencyObservationError):
                    _require_no_link_components(target, boundary)
            with mock.patch.object(Path, "lstat", side_effect=OSError("fixture")):
                with self.assertRaises(DependencyObservationError):
                    _require_no_link_components(target, boundary)

    def test_npm_package_files_reject_windows_reparse_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary).resolve()
            payload = package / "payload.js"
            payload.write_text("fixture\n", encoding="utf-8")
            original_lstat = Path.lstat

            def lstat(path: Path):
                metadata = original_lstat(path)
                if path == payload:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_file_attributes=0x0400,
                    )
                return metadata

            with (
                mock.patch.object(Path, "lstat", lstat),
                self.assertRaises(DependencyObservationError) as raised,
            ):
                _npm_package_own_files(package)
            self.assertEqual(
                raised.exception.code, "dependencies.npm-package-path-unsafe"
            )

    def test_bazel_module_parser_accepts_only_static_literal_bzlmod_intent(
        self,
    ) -> None:
        intent = _bazel_module_intent(
            {
                "source/MODULE.bazel": (
                    'module(name = "fixture_app", version = "1.0.0")\n'
                    'bazel_dep(name = "rules_cc", version = "0.1.0")\n'
                )
            }
        )
        assert intent is not None
        self.assertEqual((intent.name, intent.version), ("fixture_app", "1.0.0"))
        self.assertEqual(
            [(item.name, item.requested_version) for item in intent.dependencies],
            [("rules_cc", "0.1.0")],
        )

        rejected = (
            (
                "dynamic version",
                {
                    "source/MODULE.bazel": (
                        'VERSION = "0.1.0"\n'
                        'module(name = "fixture_app", version = "1.0.0")\n'
                        'bazel_dep(name = "rules_cc", version = VERSION)\n'
                    )
                },
            ),
            (
                "extension",
                {
                    "source/MODULE.bazel": (
                        'module(name = "fixture_app", version = "1.0.0")\n'
                        'use_extension("//:extensions.bzl", "fixture")\n'
                    )
                },
            ),
            (
                "override",
                {
                    "source/MODULE.bazel": (
                        'module(name = "fixture_app", version = "1.0.0")\n'
                        'single_version_override(module_name = "rules_cc", '
                        'version = "0.1.0")\n'
                    )
                },
            ),
            (
                "legacy workspace",
                {
                    "source/MODULE.bazel": (
                        'module(name = "fixture_app", version = "1.0.0")\n'
                    ),
                    "source/WORKSPACE": "",
                },
            ),
            (
                "cached generated lock",
                {
                    "source/MODULE.bazel": (
                        'module(name = "fixture_app", version = "1.0.0")\n'
                    ),
                    "source/MODULE.bazel.lock": "{}\n",
                },
            ),
            (
                "nested module",
                {
                    "source/nested/MODULE.bazel": (
                        'module(name = "fixture_app", version = "1.0.0")\n'
                    ),
                },
            ),
            (
                "module optional field",
                {
                    "source/MODULE.bazel": (
                        'module(name = "fixture_app", version = "1.0.0", '
                        "compatibility_level = 1)\n"
                    ),
                },
            ),
            (
                "dependency optional field",
                {
                    "source/MODULE.bazel": (
                        'module(name = "fixture_app", version = "1.0.0")\n'
                        'bazel_dep(name = "rules_cc", version = "0.1.0", '
                        'repo_name = "cc_rules")\n'
                    ),
                },
            ),
        )
        for label, files in rejected:
            with self.subTest(label=label):
                with self.assertRaises(DependencyObservationError):
                    _generated_lock_projection(files)

    def test_bzlmod_source_is_unresolved_build_intent_not_a_lock(self) -> None:
        graph = _managed_graph()
        module = (
            'module(name = "fixture_app", version = "1.0.0")\n'
            'bazel_dep(name = "rules_cc", version = "0.1.0")\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_EmptyObserver(),
                evidence_path=Path(temporary) / "resolved.cdx.json",
            )
            base_artifact = {
                "effective_revision_digest": canonical_identity(
                    {"fixture": "bazel-source-revision"}
                ).uri,
                "source_bundle_digest": canonical_identity(
                    {"fixture": "bazel-source-bundle"}
                ).uri,
                "files": {
                    "source/MODULE.bazel": module,
                    "source/main.cc": "int main() { return 0; }\n",
                },
            }
            valid = copy.deepcopy(base_artifact)
            valid["files"][CYCLONEDX_SOURCE_SBOM_PATH] = _bazel_fixture_source(
                graph
            ).decode()
            resolver.validate_source(valid)

            incomplete_without_deferred, _ = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=graph,
                composition_aggregate="incomplete_third_party_only",
            )
            no_bzlmod = copy.deepcopy(base_artifact)
            no_bzlmod["files"].pop("source/MODULE.bazel")
            no_bzlmod["files"][CYCLONEDX_SOURCE_SBOM_PATH] = (
                incomplete_without_deferred.decode()
            )
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.validate_source(no_bzlmod)
            self.assertEqual(
                caught.exception.code,
                "dependencies.source-composition-unjustified",
            )

            unrelated_ref = "pkg:generic/source-generator"
            unrelated_source, _ = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=graph,
                additional_components=(
                    {
                        "type": "library",
                        "bom-ref": "pkg:generic/rules_cc",
                        "name": "rules_cc",
                        "purl": "pkg:generic/rules_cc",
                        "isExternal": True,
                        "versionRange": "vers:generic/>=0.1.0",
                        "properties": [
                            {
                                "name": "literate-ai:dependency-kind",
                                "value": "build",
                            },
                            {
                                "name": "literate-ai:dependency-scope",
                                "value": "build",
                            },
                            {
                                "name": "literate-ai:bzlmod-requested-version",
                                "value": "0.1.0",
                            },
                        ],
                    },
                    {
                        "type": "library",
                        "bom-ref": unrelated_ref,
                        "name": "source-generator",
                        "purl": unrelated_ref,
                        "isExternal": True,
                        "versionRange": "vers:generic/>=1.0.0",
                        "properties": [
                            {
                                "name": "literate-ai:dependency-kind",
                                "value": "build",
                            },
                            {
                                "name": "literate-ai:dependency-scope",
                                "value": "build",
                            },
                        ],
                    },
                ),
                additional_edges=(
                    (graph.root_ref, "pkg:generic/rules_cc"),
                    (graph.root_ref, unrelated_ref),
                ),
                composition_aggregate="incomplete_third_party_only",
            )
            unrelated = copy.deepcopy(base_artifact)
            unrelated["files"][CYCLONEDX_SOURCE_SBOM_PATH] = unrelated_source.decode()
            resolver.validate_source(unrelated)

            complete = copy.deepcopy(base_artifact)
            complete["files"][CYCLONEDX_SOURCE_SBOM_PATH] = _bazel_fixture_source(
                graph, aggregate="complete"
            ).decode()
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.validate_source(complete)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-source-composition-invalid",
            )

            locked = copy.deepcopy(base_artifact)
            locked["files"][CYCLONEDX_SOURCE_SBOM_PATH] = _bazel_fixture_source(
                graph, exact=True
            ).decode()
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.validate_source(locked)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-source-intent-invalid"
            )

            missing_direct = copy.deepcopy(base_artifact)
            missing_direct_document = json.loads(_bazel_fixture_source(graph))
            missing_ref = next(
                item["bom-ref"]
                for item in missing_direct_document["components"]
                if item["name"] == "rules_cc"
            )
            missing_direct_document["components"] = [
                item
                for item in missing_direct_document["components"]
                if item.get("bom-ref") != missing_ref
            ]
            for edge in missing_direct_document["dependencies"]:
                if edge["ref"] == graph.root_ref:
                    edge["dependsOn"].remove(missing_ref)
            missing_direct_document["dependencies"] = [
                edge
                for edge in missing_direct_document["dependencies"]
                if edge["ref"] != missing_ref
            ]
            missing_direct["files"][CYCLONEDX_SOURCE_SBOM_PATH] = canonical_json_bytes(
                missing_direct_document
            ).decode()
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.validate_source(missing_direct)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-source-intent-invalid"
            )

            wrong_type = copy.deepcopy(base_artifact)
            wrong_type_document = json.loads(_bazel_fixture_source(graph))
            next(
                item
                for item in wrong_type_document["components"]
                if item["name"] == "rules_cc"
            )["type"] = "framework"
            wrong_type["files"][CYCLONEDX_SOURCE_SBOM_PATH] = canonical_json_bytes(
                wrong_type_document
            ).decode()
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.validate_source(wrong_type)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-source-intent-invalid"
            )

    def test_bzlmod_observation_resolves_full_reachable_graph(self) -> None:
        graph = _managed_graph()
        source_bundle = canonical_identity({"fixture": "bazel-source"}).uri
        toolchain = canonical_identity({"fixture": "native-toolchain"}).uri
        source_document = json.loads(_bazel_fixture_source(graph))
        rules_source = next(
            item for item in source_document["components"] if item["name"] == "rules_cc"
        )
        rules_source["versionRange"] = "vers:generic/>=999.0.0"
        requested_property = next(
            item
            for item in rules_source["properties"]
            if item["name"] == "literate-ai:bzlmod-requested-version"
        )
        requested_property["value"] = "999.0.0"
        source = canonical_json_bytes(source_document)
        module = (
            'module(name = "fixture_app", version = "1.0.0")\n'
            'bazel_dep(name = "rules_cc", version = "999.0.0")\n'
        )
        artifact = {
            "effective_revision_digest": canonical_identity(
                {"fixture": "bazel-revision"}
            ).uri,
            "source_bundle_digest": source_bundle,
            "files": {
                CYCLONEDX_SOURCE_SBOM_PATH: source.decode(),
                "source/MODULE.bazel": module,
                "source/main.cc": "int main() { return 0; }\n",
            },
        }
        observation = _bzlmod_observation(
            source_bundle_digest=source_bundle,
            toolchain_identity=toolchain,
        )
        build: dict[str, object] = {
            "source_bundle_digest": source_bundle,
            "authorization_id": "authorization:fixture",
            "toolchain_identity": toolchain,
            "dependency_observation": observation,
        }
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            artifact_path = temporary_root / "artifact"
            artifact_path.mkdir()
            _write_bzlmod_composite(
                artifact_path,
                build=build,
                observation=observation,
            )
            evidence_artifact = (
                artifact_path / ".literate" / "bazel" / "module-graph.json"
            )
            evidence_path = temporary_root / "resolved.cdx.json"
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_EmptyObserver(),
                evidence_path=evidence_path,
            )
            path_type = type(artifact_path)
            with mock.patch.object(
                path_type,
                "__lt__",
                lambda left, right: str(left).casefold() < str(right).casefold(),
            ):
                resolver.resolve(artifact, build)
            document = json.loads(evidence_path.read_bytes())
            inventory = {item["bom-ref"]: item for item in document["components"]}
            self.assertEqual(inventory["pkg:generic/rules_cc"]["version"], "0.2.0")
            rules_properties = {
                item["name"]: item["value"]
                for item in inventory["pkg:generic/rules_cc"]["properties"]
            }
            self.assertEqual(
                rules_properties["literate-ai:bzlmod-selected-version"], "0.2.0"
            )
            self.assertEqual(
                inventory["pkg:generic/platforms@0.0.10"]["version"], "0.0.10"
            )
            edges = {
                (item["ref"], target)
                for item in document["dependencies"]
                for target in item["dependsOn"]
            }
            self.assertIn((graph.root_ref, "pkg:generic/rules_cc"), edges)
            self.assertIn(
                ("pkg:generic/rules_cc", "pkg:generic/platforms@0.0.10"), edges
            )
            self.assertIn(
                ("pkg:generic/platforms@0.0.10", "pkg:generic/rules_cc"), edges
            )

            buildfiles_path = artifact_path / ".literate" / "bazel" / "buildfiles.txt"
            buildfiles_path.write_bytes(b"//:BUILD.bazel\n")
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-build-inputs-mismatch"
            )
            buildfiles_path.write_bytes(b"")

            consumption_path = (
                artifact_path / ".literate" / "bazel" / "build-input-consumption.json"
            )
            original_consumption = consumption_path.read_bytes()
            consumption_path.write_bytes(b"{}")
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-build-inputs-content-mismatch",
            )
            consumption_path.write_bytes(original_consumption)

            mismatched_consumption = copy.deepcopy(build)
            mismatched_consumption["build_input_consumption_identity"] = (
                canonical_identity({"fixture": "wrong-consumption"}).uri
            )
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, mismatched_consumption)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-build-inputs-authority-mismatch",
            )

            missing_observation = dict(build)
            missing_observation.pop("dependency_observation")
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, missing_observation)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-observation-missing"
            )

            tampered = copy.deepcopy(build)
            tampered["dependency_observation"]["modules"][0]["version"] = "0.0.11"
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, tampered)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-observation-invalid",
            )

            manifest_path = artifact_path / "build-manifest.json"
            original_manifest = manifest_path.read_bytes()
            incomplete_manifest = json.loads(original_manifest)
            incomplete_manifest["files"].pop()
            incomplete_manifest_bytes = canonical_json_bytes(incomplete_manifest)
            manifest_path.write_bytes(incomplete_manifest_bytes)
            build["artifact_digest"] = (
                "sha256:" + hashlib.sha256(incomplete_manifest_bytes).hexdigest()
            )
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-manifest-files-mismatch",
            )
            manifest_path.write_bytes(original_manifest)
            build["artifact_digest"] = (
                "sha256:" + hashlib.sha256(original_manifest).hexdigest()
            )

            evidence_artifact.write_bytes(b"changed graph\n")
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-evidence-content-mismatch",
            )

            evidence_artifact.unlink()
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-evidence-missing"
            )

            outside = temporary_root / "outside.json"
            outside.write_bytes(b"fixture graph\n")
            evidence_artifact.symlink_to(outside)
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-evidence-unsafe"
            )

            evidence_artifact.unlink()
            evidence_artifact.mkdir()
            with self.assertRaises(DependencyObservationError):
                resolver.resolve(artifact, build)

    def test_dependency_free_bzlmod_still_requires_raw_build_evidence(self) -> None:
        graph = _managed_graph()
        source, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
        )
        source_bundle = canonical_identity({"fixture": "empty-bzlmod-source"}).uri
        artifact = {
            "effective_revision_digest": canonical_identity(
                {"fixture": "empty-bzlmod-revision"}
            ).uri,
            "source_bundle_digest": source_bundle,
            "files": {
                CYCLONEDX_SOURCE_SBOM_PATH: source.decode(),
                "source/MODULE.bazel": (
                    'module(name = "fixture_app", version = "1.0.0")\n'
                ),
                "source/main.cc": "int main() { return 0; }\n",
            },
        }
        build = {
            "source_bundle_digest": source_bundle,
            "authorization_id": "authorization:fixture",
            "toolchain_identity": canonical_identity(
                {"fixture": "native-toolchain"}
            ).uri,
        }
        with tempfile.TemporaryDirectory() as temporary:
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_EmptyObserver(),
                evidence_path=Path(temporary) / "resolved.cdx.json",
            )
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
        self.assertEqual(
            caught.exception.code, "dependencies.bzlmod-observation-missing"
        )

    def test_bzlmod_raw_evidence_must_independently_derive_observation(self) -> None:
        contents = _bzlmod_evidence_contents()
        observation = BuildDependencyObservation.from_dict(
            _bzlmod_observation(
                source_bundle_digest=canonical_identity({"fixture": "source"}).uri,
                toolchain_identity=canonical_identity({"fixture": "toolchain"}).uri,
            )
        )

        graph = json.loads(contents[".literate/bazel/module-graph.json"])
        graph["dependencies"] = []
        with self.assertRaises(DependencyObservationError) as caught:
            _require_raw_bazel_graph(
                json.dumps(graph).encode(), observation=observation
            )
        self.assertEqual(
            caught.exception.code,
            "dependencies.bzlmod-graph-observation-mismatch",
        )

        lock = json.loads(contents[".literate/bazel/MODULE.bazel.lock"])
        lock["registryFileHashes"].pop(
            "https://bcr.example/modules/rules_cc/0.2.0/source.json"
        )
        with self.assertRaises(DependencyObservationError) as caught:
            _require_raw_bazel_lock(
                json.dumps(lock).encode(), modules=observation.modules
            )
        self.assertEqual(
            caught.exception.code,
            "dependencies.bzlmod-lock-module-missing",
        )

        records = [
            json.loads(line)
            for line in contents[".literate/bazel/repositories.ndjson"].splitlines()
        ]
        records[1]["attribute"] = [
            item for item in records[1]["attribute"] if item["name"] != "integrity"
        ]
        repositories = b"".join(
            json.dumps(record).encode() + b"\n" for record in records
        )
        with self.assertRaises(DependencyObservationError) as caught:
            _require_raw_bazel_repositories(repositories, modules=observation.modules)
        self.assertEqual(
            caught.exception.code,
            "dependencies.bzlmod-repository-module-missing",
        )

    def test_supported_vers_schemes_reject_out_of_range_resolutions(self) -> None:
        for scheme in ("npm", "cargo", "pypi", "generic"):
            version_range = f"vers:{scheme}/>=1.2.0|<2.0.0"
            with self.subTest(scheme=scheme):
                _require_version_in_range(version_range, "1.5.0")
                with self.assertRaises(DependencyObservationError) as caught:
                    _require_version_in_range(version_range, "99.0.0")
                self.assertEqual(
                    caught.exception.code, "dependencies.source-range-mismatch"
                )
        _require_version_in_range("vers:cargo/>=0.8.8,<0.9", "0.8.9")
        with self.assertRaises(DependencyObservationError) as caught:
            _require_version_in_range("vers:cargo/>=0.8.8,<0.9", "1.0.0")
        self.assertEqual(caught.exception.code, "dependencies.source-range-mismatch")
        with self.assertRaises(DependencyObservationError) as caught:
            _require_version_in_range("vers:unsupported/>=1|<2", "1.5")
        self.assertEqual(caught.exception.code, "dependencies.source-range-unsupported")

    def test_resolver_writes_strict_continuous_evidence_before_returning(self) -> None:
        graph = _managed_graph()
        source_content, source_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
        )
        revision = canonical_identity({"fixture": "revision"}).uri
        source_bundle = canonical_identity({"fixture": "source"}).uri
        artifact_digest = canonical_identity({"fixture": "artifact"}).uri
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "artifact"
            artifact_path.mkdir()
            evidence_path = root / "evidence" / "resolved.cdx.json"
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_ObservedRuntime(),
                evidence_path=evidence_path,
            )
            artifact = {
                "effective_revision_digest": revision,
                "source_bundle_digest": source_bundle,
                "files": {
                    CYCLONEDX_SOURCE_SBOM_PATH: source_content.decode(),
                    "source/main.py": "print('portable')\n",
                },
            }
            source_validation = resolver.validate_source(artifact)
            result = resolver.resolve(
                artifact,
                {
                    "source_bundle_digest": source_bundle,
                    "artifact_digest": artifact_digest,
                    "artifact_path": str(artifact_path),
                },
            )

            self.assertEqual(
                evidence_path.read_bytes().rstrip(), evidence_path.read_bytes()
            )
            self.assertEqual(result["source_bom"], source_binding.to_dict())
            normalized = require_dependency_resolution_result(
                result,
                resolver_id=resolver.resolver_id,
                effective_revision_digest=revision,
                source_bundle_digest=source_bundle,
                artifact_digest=artifact_digest,
                source_bom_identity=source_binding.bom_identity.uri,
                source_validation=source_validation,
                managed_graph_identity=graph.identity.uri,
                composition_identity=graph.resolved_graph_identity.uri,
                root_ref=graph.root_ref,
            )
            self.assertEqual(
                normalized["resolved_bom"]["source_bom_identity"],
                source_binding.bom_identity.to_dict(),
            )

            tampered = copy.deepcopy(result)
            tampered["resolved_bom"]["source_bom_identity"] = canonical_identity(
                {"fixture": "another-source-bom"}
            ).to_dict()
            with self.assertRaises(PortContractError) as caught:
                require_dependency_resolution_result(
                    tampered,
                    resolver_id=resolver.resolver_id,
                    effective_revision_digest=revision,
                    source_bundle_digest=source_bundle,
                    artifact_digest=artifact_digest,
                    source_bom_identity=source_binding.bom_identity.uri,
                    source_validation=source_validation,
                    managed_graph_identity=graph.identity.uri,
                    composition_identity=graph.resolved_graph_identity.uri,
                    root_ref=graph.root_ref,
                )
            self.assertEqual(
                caught.exception.code,
                "ports.dependencies.source-transition-mismatch",
            )

    def test_resolver_preserves_source_package_edge_and_uses_exact_lock(self) -> None:
        graph = _managed_graph()
        package_ref = "pkg:pypi/requests"
        source_content, _source_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(
                {
                    "type": "library",
                    "bom-ref": package_ref,
                    "name": "requests",
                    "purl": "pkg:pypi/requests",
                    "isExternal": True,
                    "versionRange": "vers:generic/>=2.32.0|<3.0.0",
                    "properties": [
                        {
                            "name": "literate-ai:dependency-kind",
                            "value": "package",
                        },
                        {
                            "name": "literate-ai:dependency-scope",
                            "value": "runtime",
                        },
                    ],
                },
            ),
            additional_edges=((graph.root_ref, package_ref),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "artifact"
            artifact_path.mkdir()
            evidence_path = root / "resolved.cdx.json"
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_ObservedRuntime(),
                evidence_path=evidence_path,
                additional_components=(
                    {
                        "type": "library",
                        "bom-ref": package_ref,
                        "name": "requests",
                        "version": "2.32.0",
                        "purl": "pkg:pypi/requests@2.32.0",
                        "properties": [
                            {
                                "name": "literate-ai:dependency-kind",
                                "value": "package",
                            },
                            {
                                "name": "literate-ai:dependency-scope",
                                "value": "runtime",
                            },
                        ],
                    },
                ),
            )
            resolver.resolve(
                {
                    "effective_revision_digest": canonical_identity(
                        {"fixture": "package-revision"}
                    ).uri,
                    "source_bundle_digest": canonical_identity(
                        {"fixture": "package-source"}
                    ).uri,
                    "files": {
                        CYCLONEDX_SOURCE_SBOM_PATH: source_content.decode(),
                        "source/main.py": "import requests\n",
                    },
                },
                {
                    "source_bundle_digest": canonical_identity(
                        {"fixture": "package-source"}
                    ).uri,
                    "artifact_digest": canonical_identity(
                        {"fixture": "package-artifact"}
                    ).uri,
                    "artifact_path": str(artifact_path),
                },
            )
            resolved = json.loads(evidence_path.read_bytes())
            package = next(
                item
                for item in resolved["components"]
                if item["bom-ref"] == package_ref
            )
            self.assertEqual(package["version"], "2.32.0")
            self.assertNotIn("versionRange", package)
            root_dependencies = next(
                item
                for item in resolved["dependencies"]
                if item["ref"] == graph.root_ref
            )
            self.assertIn(package_ref, root_dependencies["dependsOn"])

    def test_npm_lock_projects_integrity_and_parent_edges_exactly(self) -> None:
        graph = _managed_graph()
        alpha_ref = "pkg:npm/alpha"
        beta_ref = "pkg:npm/beta"

        def package(ref: str, name: str) -> dict[str, object]:
            return {
                "type": "library",
                "bom-ref": ref,
                "name": name,
                "purl": f"pkg:npm/{name}",
                "isExternal": True,
                "versionRange": "vers:npm/>=1.0.0|<2.0.0",
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "package"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                ],
            }

        source_content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(
                package(alpha_ref, "alpha"),
                package(beta_ref, "beta"),
            ),
            additional_edges=(
                (graph.root_ref, alpha_ref),
                (alpha_ref, beta_ref),
            ),
        )
        integrity = "sha256-" + base64.b64encode(b"a" * 32).decode()
        package_lock = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "fixture-app",
                        "version": "1.0.0",
                        "dependencies": {"alpha": "1.1.0"},
                    },
                    "node_modules/alpha": {
                        "version": "1.1.0",
                        "integrity": integrity,
                        "dependencies": {"beta": "1.2.0"},
                        "peerDependencies": {"missing-peer": ">=3.0.0"},
                        "peerDependenciesMeta": {"missing-peer": {"optional": True}},
                    },
                    "node_modules/beta": {
                        "version": "1.2.0",
                        "integrity": integrity,
                    },
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "artifact"
            artifact_path.mkdir()
            evidence_path = root / "resolved.cdx.json"
            CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_ObservedRuntime(),
                evidence_path=evidence_path,
            ).resolve(
                {
                    "effective_revision_digest": canonical_identity(
                        {"fixture": "npm-revision"}
                    ).uri,
                    "source_bundle_digest": canonical_identity(
                        {"fixture": "npm-source"}
                    ).uri,
                    "files": {
                        CYCLONEDX_SOURCE_SBOM_PATH: source_content.decode(),
                        "source/package.json": json.dumps(
                            {
                                "name": "fixture-app",
                                "dependencies": {"alpha": "1.1.0"},
                            }
                        ),
                        "source/package-lock.json": package_lock,
                        "source/main.js": "import alpha from 'alpha';\n",
                    },
                },
                {
                    "source_bundle_digest": canonical_identity(
                        {"fixture": "npm-source"}
                    ).uri,
                    "artifact_digest": canonical_identity(
                        {"fixture": "npm-artifact"}
                    ).uri,
                    "artifact_path": str(artifact_path),
                },
            )
            inventory = {
                item["bom-ref"]: item
                for item in json.loads(evidence_path.read_bytes())["components"]
            }
            self.assertEqual(inventory[alpha_ref]["version"], "1.1.0")
            self.assertEqual(inventory[beta_ref]["version"], "1.2.0")
            self.assertEqual(inventory[beta_ref]["hashes"][0]["alg"], "SHA-256")

        lock_projection = _generated_lock_projection(
            {
                "source/package.json": json.dumps(
                    {
                        "name": "fixture-app",
                        "dependencies": {"alpha": "1.1.0"},
                    }
                ),
                "source/package-lock.json": package_lock,
            }
        )
        missing_edge_source, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(
                package(alpha_ref, "alpha"),
                package(beta_ref, "beta"),
            ),
            additional_edges=(
                (graph.root_ref, alpha_ref),
                (graph.root_ref, beta_ref),
            ),
        )
        with self.assertRaises(DependencyObservationError) as caught:
            _resolved_source_projection(
                missing_edge_source,
                managed_graph=graph,
                resolution_candidates=(),
                lock_projection=lock_projection,
            )
        self.assertEqual(caught.exception.code, "dependencies.lock-edge-missing")

        wrong_ecosystem = package(beta_ref, "beta")
        wrong_ecosystem["purl"] = "pkg:cargo/beta"
        wrong_ecosystem["versionRange"] = "vers:cargo/>=1.0.0|<2.0.0"
        wrong_ecosystem_source, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(package(alpha_ref, "alpha"), wrong_ecosystem),
            additional_edges=(
                (graph.root_ref, alpha_ref),
                (alpha_ref, beta_ref),
            ),
        )
        with self.assertRaises(DependencyObservationError) as caught:
            _resolved_source_projection(
                wrong_ecosystem_source,
                managed_graph=graph,
                resolution_candidates=(),
                lock_projection=lock_projection,
            )
        self.assertEqual(caught.exception.code, "dependencies.source-range-unresolved")

    def test_npm_source_dependency_observation_excludes_package_engines_field(
        self,
    ) -> None:
        """package.json ``engines`` is Node toolchain metadata, not a package."""

        graph = _managed_graph()
        package_ref = "pkg:npm/pg"
        node_engine_ref = "pkg:generic/node"

        package_component = {
            "type": "library",
            "bom-ref": package_ref,
            "name": "pg",
            "purl": package_ref,
            "isExternal": True,
            "versionRange": "vers:npm/>=8.23.0|<8.23.1",
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "package"},
                {"name": "literate-ai:dependency-scope", "value": "runtime"},
            ],
        }
        node_engine_component = {
            "type": "library",
            "bom-ref": node_engine_ref,
            "name": "node",
            "purl": node_engine_ref,
            "isExternal": True,
            "versionRange": "vers:generic/>=20",
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "toolchain"},
                {"name": "literate-ai:dependency-scope", "value": "build"},
            ],
        }
        source_content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(package_component, node_engine_component),
            additional_edges=(
                (graph.root_ref, package_ref),
                (graph.root_ref, node_engine_ref),
            ),
        )
        integrity = "sha256-" + base64.b64encode(b"a" * 32).decode()
        package_lock = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "fixture-app",
                        "version": "1.0.0",
                        "dependencies": {"pg": "8.23.0"},
                        "engines": {"node": ">=20"},
                    },
                    "node_modules/pg": {
                        "version": "8.23.0",
                        "integrity": integrity,
                        "engines": {"node": ">=20"},
                    },
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_EmptyObserver(),
                evidence_path=Path(temporary) / "resolved.cdx.json",
            )
            artifact = {
                "effective_revision_digest": canonical_identity(
                    {"fixture": "npm-engines-revision"}
                ).uri,
                "source_bundle_digest": canonical_identity(
                    {"fixture": "npm-engines-source"}
                ).uri,
                "files": {
                    CYCLONEDX_SOURCE_SBOM_PATH: source_content.decode(),
                    "source/package.json": json.dumps(
                        {
                            "name": "fixture-app",
                            "dependencies": {"pg": "8.23.0"},
                            "engines": {"node": ">=20"},
                        }
                    ),
                    "source/package-lock.json": package_lock,
                    "source/main.js": "import pg from 'pg';\n",
                },
            }
            # Must not raise: an unpinned engines.node toolchain declaration is
            # runtime/toolchain compatibility metadata, not an npm package
            # dependency requiring an exact lock or observation entry.
            resolver.validate_source(artifact)

    def test_cargo_resolver_uses_post_authorization_derived_lock(self) -> None:
        graph = _managed_graph()
        serde_ref = "pkg:cargo/serde"
        source_content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(
                {
                    "type": "library",
                    "bom-ref": "pkg:cargo/serde",
                    "name": "serde",
                    "versionRange": "vers:cargo/>=1.0.0|<2.0.0",
                    "purl": "pkg:cargo/serde",
                    "isExternal": True,
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "package"},
                        {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    ],
                },
            ),
            additional_edges=((graph.root_ref, "pkg:cargo/serde"),),
        )
        source_identity = canonical_identity({"fixture": "cargo-source"}).uri
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact"
            artifact.mkdir()
            evidence = root / "resolved.cdx.json"
            result = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_ObservedRuntime(),
                evidence_path=evidence,
                allow_missing_cargo_lock=True,
            ).resolve(
                {
                    "effective_revision_digest": canonical_identity(
                        {"fixture": "cargo-revision"}
                    ).uri,
                    "source_bundle_digest": source_identity,
                    "files": {
                        CYCLONEDX_SOURCE_SBOM_PATH: source_content.decode(),
                        "source/Cargo.toml": (
                            '[package]\nname="fixture"\nversion="1.0.0"\n'
                            '[dependencies]\nserde="1"\n'
                        ),
                        "source/main.rs": "fn main() {}\n",
                    },
                },
                {
                    "source_bundle_digest": source_identity,
                    "artifact_digest": canonical_identity(
                        {"fixture": "cargo-artifact"}
                    ).uri,
                    "artifact_path": str(artifact),
                    "cargo_lock": {
                        "path": "source/Cargo.lock",
                        "content": (
                            "version = 3\n\n"
                            '[[package]]\nname="fixture"\nversion="1.0.0"\n'
                            'dependencies=["serde"]\n\n'
                            '[[package]]\nname="serde"\nversion="1.0.228"\n'
                            'source="registry+https://github.com/rust-lang/'
                            'crates.io-index"\n'
                            f'checksum="{"a" * 64}"\n'
                        ),
                    },
                },
            )

            resolved = json.loads(evidence.read_bytes())

        serde = next(
            item for item in resolved["components"] if item["bom-ref"] == serde_ref
        )
        self.assertEqual(serde["version"], "1.0.228")
        self.assertEqual(serde["hashes"][0]["content"], "a" * 64)
        self.assertEqual(result["source_bundle_digest"], source_identity)

    def test_cargo_resolver_requires_derived_lock_at_resolution(self) -> None:
        graph = _managed_graph()
        source_content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
        )
        source_identity = canonical_identity({"fixture": "cargo-missing-lock"}).uri
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "artifact"
            artifact.mkdir()
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_ObservedRuntime(),
                evidence_path=Path(temporary) / "resolved.cdx.json",
                allow_missing_cargo_lock=True,
            )
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(
                    {
                        "effective_revision_digest": canonical_identity(
                            {"fixture": "cargo-revision"}
                        ).uri,
                        "source_bundle_digest": source_identity,
                        "files": {
                            CYCLONEDX_SOURCE_SBOM_PATH: source_content.decode(),
                            "source/Cargo.toml": (
                                '[package]\nname="fixture"\nversion="1.0.0"\n'
                            ),
                        },
                    },
                    {
                        "source_bundle_digest": source_identity,
                        "artifact_digest": canonical_identity(
                            {"fixture": "cargo-artifact"}
                        ).uri,
                        "artifact_path": str(artifact),
                    },
                )
        self.assertEqual(caught.exception.code, "dependencies.rust-build-lock-missing")

    def test_language_manifests_imports_and_locks_reconcile_with_bom(self) -> None:
        fixtures = (
            (
                "python",
                {
                    "source/requirements.txt": "requests==2.32.0\n",
                    "source/main.py": (
                        "import requests\nimport helper\n"
                        "from tests.litai_test import run_all\n"
                    ),
                    "source/helper.py": "VALUE = 1\n",
                    "source/tests/litai_test.py": "def run_all(): return []\n",
                },
                _inventory("requests"),
            ),
            (
                "javascript",
                {
                    "source/package.json": json.dumps(
                        {
                            "name": "fixture-js",
                            "dependencies": {"lodash": "4.17.21"},
                        }
                    ),
                    "source/package-lock.json": json.dumps(
                        {
                            "packages": {
                                "": {"name": "fixture-js"},
                                "node_modules/lodash": {"name": "lodash"},
                            },
                            "dependencies": {"lodash": {"version": "4.17.21"}},
                        }
                    ),
                    "source/main.js": "import lodash from 'lodash';\n",
                },
                _inventory("lodash"),
            ),
            (
                "rust",
                {
                    "source/Cargo.toml": (
                        '[package]\nname="fixture-rust"\nversion="1.0.0"\n'
                        '[dependencies]\nserde="1"\n'
                    ),
                    "source/Cargo.lock": (
                        '[[package]]\nname="fixture-rust"\nversion="1.0.0"\n'
                        '[[package]]\nname="serde"\nversion="1.0.0"\n'
                    ),
                    "source/main.rs": "use serde::Serialize;\nfn main() {}\n",
                },
                _inventory("serde"),
            ),
            (
                "cpp",
                {
                    "source/main.cpp": '#include "portable.hpp"\nint main() {}\n',
                    "source/portable.hpp": "#pragma once\n",
                },
                _inventory(),
            ),
        )
        for language, files, inventory in fixtures:
            with self.subTest(language=language):
                reconcile_generated_dependencies(files, inventory)

        with self.assertRaises(DependencyObservationError) as caught:
            reconcile_generated_dependencies(
                {
                    "source/package.json": json.dumps(
                        {"dependencies": {"missing-package": "1.0.0"}}
                    ),
                    "source/main.js": "require('missing-package');\n",
                },
                _inventory(),
            )
        self.assertEqual(caught.exception.code, "dependencies.manifest-bom-mismatch")

    def test_cargo_manifest_rejects_path_and_git_dependencies(self) -> None:
        for dependency in (
            'serde = { path = "../serde" }\n',
            'serde = { git = "https://example.invalid/serde" }\n',
        ):
            with self.subTest(dependency=dependency):
                with self.assertRaises(DependencyObservationError) as caught:
                    _generated_lock_projection(
                        {
                            "source/Cargo.toml": (
                                '[package]\nname="fixture"\nversion="1.0.0"\n'
                                "[dependencies]\n" + dependency
                            )
                        },
                        allow_missing_cargo_lock=True,
                    )
                self.assertEqual(
                    caught.exception.code, "dependencies.rust-authority-unsupported"
                )

    def test_cargo_manifest_can_defer_lock_to_authorized_build_projection(self) -> None:
        files = {
            "source/Cargo.toml": (
                '[package]\nname="fixture-rust"\nversion="1.0.0"\n'
                '[dependencies]\nserde="1"\n'
            )
        }

        projection = _generated_lock_projection(files, allow_missing_cargo_lock=True)
        reconcile_generated_dependencies(
            files,
            _inventory("serde"),
            allow_missing_cargo_lock=True,
        )

        self.assertEqual(projection.packages, ())
        with self.assertRaises(DependencyObservationError) as caught:
            _generated_lock_projection(files)
        self.assertEqual(caught.exception.code, "dependencies.rust-lock-missing")

    def test_cargo_lock_projects_checksummed_registry_graph(self) -> None:
        projection = _generated_lock_projection(
            {
                "source/Cargo.toml": (
                    '[package]\nname="fixture-rust"\nversion="1.0.0"\n'
                    '[dependencies]\nserde="1"\n'
                ),
                "source/Cargo.lock": (
                    "version = 3\n\n"
                    '[[package]]\nname = "fixture-rust"\nversion = "1.0.0"\n'
                    'dependencies = ["serde"]\n\n'
                    '[[package]]\nname = "serde"\nversion = "1.0.228"\n'
                    'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                    f'checksum = "{"a" * 64}"\n'
                ),
            }
        )

        self.assertEqual(
            {(item.coordinate, item.version) for item in projection.packages},
            {("fixture-rust", "1.0.0"), ("serde", "1.0.228")},
        )
        self.assertEqual(len(projection.edges), 1)
        self.assertIn("fixture-rust@1.0.0", projection.edges[0][0])
        self.assertIn("serde@1.0.228", projection.edges[0][1])

    def test_cpp_local_include_resolution_normalizes_safe_parent_segments(self) -> None:
        reconcile_generated_dependencies(
            {
                "source/api/validation.hpp": "#pragma once\n",
                "source/worker/processing.hpp": (
                    '#include "../api/validation.hpp"\n#pragma once\n'
                ),
                "source/main.cpp": (
                    '#include "api/validation.hpp"\n'
                    '#include "worker/processing.hpp"\n'
                    "int main() {}\n"
                ),
            },
            _inventory(),
        )

        with self.assertRaises(DependencyObservationError) as caught:
            reconcile_generated_dependencies(
                {
                    "source/main.cpp": '#include "../outside.hpp"\nint main() {}\n',
                },
                _inventory(),
            )
        self.assertEqual(
            caught.exception.code, "dependencies.cpp-local-include-missing"
        )

    def test_runtime_python_import_requires_explicit_resolver_authority(self) -> None:
        files = {"source/main.py": "from literate_ai_native_sdk import binding\n"}
        with self.assertRaises(DependencyObservationError) as caught:
            reconcile_generated_dependencies(files, _inventory())
        self.assertEqual(caught.exception.code, "dependencies.import-bom-mismatch")
        supplied = frozenset({"literate_ai_native_sdk"})
        reconcile_generated_dependencies(
            files, _inventory(), runtime_python_imports=supplied
        )
        for extra, code in (
            (
                {"source/requirements.txt": "literate-ai-native-sdk==1\n"},
                "dependencies.manifest-bom-mismatch",
            ),
            (
                {"source/other.py": "import undeclared_package\n"},
                "dependencies.import-bom-mismatch",
            ),
            (
                {"source/index.js": "import sdk from 'literate-ai-native-sdk';\n"},
                "dependencies.import-bom-mismatch",
            ),
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaises(DependencyObservationError) as caught,
            ):
                reconcile_generated_dependencies(
                    {**files, **extra}, _inventory(), runtime_python_imports=supplied
                )
            self.assertEqual(caught.exception.code, code)

    def test_python_distribution_can_declare_a_distinct_top_level_import(self) -> None:
        inventory = json.loads(_inventory("cupy-cuda13x"))
        inventory["components"][0].setdefault("properties", []).append(
            {
                "name": "literate-ai:python-top-level-import",
                "value": "cupy",
            }
        )
        reconcile_generated_dependencies(
            {
                "source/requirements.txt": "cupy-cuda13x==14.1.1\n",
                "source/main.py": "import cupy\n",
            },
            canonical_json_bytes(inventory),
        )

        inventory["components"][0]["properties"] = []
        with self.assertRaises(DependencyObservationError) as caught:
            reconcile_generated_dependencies(
                {
                    "source/requirements.txt": "cupy-cuda13x==14.1.1\n",
                    "source/main.py": "import cupy\n",
                },
                canonical_json_bytes(inventory),
            )
        self.assertEqual(caught.exception.code, "dependencies.import-bom-mismatch")

    def test_javascript_sibling_modules_are_not_npm_dependencies(self) -> None:
        # A generated entrypoint importing a sibling by bare specifier resolves
        # to a generated file. Treating "main" and "litai_test" as npm packages
        # failed the whole lifecycle for an application with no dependencies.
        reconcile_generated_dependencies(
            {
                "source/index.js": (
                    "import { main } from 'main';\nimport { run } from 'litai_test';\n"
                ),
                "source/main.js": "export const main = () => {};\n",
                "source/litai_test.js": "export const run = () => {};\n",
            },
            _inventory(),
        )

        with self.assertRaises(DependencyObservationError) as caught:
            reconcile_generated_dependencies(
                {
                    "source/index.js": (
                        "import { main } from 'main';\nimport x from 'lodash';\n"
                    ),
                    "source/main.js": "export const main = () => {};\n",
                },
                _inventory(),
            )
        self.assertEqual(caught.exception.code, "dependencies.import-bom-mismatch")
        self.assertIn("lodash", str(caught.exception))

    def test_javascript_import_admission_ignores_inert_source_text(self) -> None:
        build_helper = (
            'import fs from "node:fs";\n'
            'import path from "node:path";\n\n'
            "function moduleBody(source) {\n"
            "  return source\n"
            r'    .replace(/^import ([A-Za-z_$][\w$]*) from "(node:[^"]+)";$/gm, '
            "'const $1 = require(\"$2\");')\n"
            r'    .replace(/^import .* from "\.{1,2}\/.*";\n/gm, "");'
            "\n}\n\n"
            "const quoted = 'require(\"not-a-package\")';\n"
            'const template = `import("also-not-a-package")`;\n'
            "const divided = 10 / 2;\n"
            '// require("comment-only-package")\n'
            '/* import("block-comment-only-package") */\n'
        )
        reconcile_generated_dependencies(
            {"source/tools/build.js": build_helper},
            _inventory(),
        )

    def test_javascript_import_admission_keeps_executable_forms(self) -> None:
        with self.assertRaises(DependencyObservationError) as caught:
            reconcile_generated_dependencies(
                {
                    "source/main.js": (
                        'import "side-effect-package";\n'
                        'import value from "declared-package";\n'
                        'export { value } from "exported-package/subpath";\n'
                        'const lazy = import("dynamic-package");\n'
                        'const common = require("common-package");\n'
                    )
                },
                _inventory(),
            )
        self.assertEqual(caught.exception.code, "dependencies.import-bom-mismatch")
        for name in (
            "common-package",
            "declared-package",
            "dynamic-package",
            "exported-package",
            "side-effect-package",
        ):
            self.assertIn(name, str(caught.exception))

    def test_dunder_main_is_never_an_external_dependency(self) -> None:
        # `__main__` always exists at runtime but is absent from
        # sys.stdlib_module_names, so a zipapp entrypoint importing it was
        # reported as an undeclared external dependency named "-main-".
        reconcile_generated_dependencies(
            {
                "source/main.py": "import __main__\n",
                "source/__main__.py": "from __main__ import main\n",
            },
            _inventory(),
        )

        with self.assertRaises(DependencyObservationError) as caught:
            reconcile_generated_dependencies(
                {"source/main.py": "import __main__\nimport requests\n"},
                _inventory(),
            )
        self.assertEqual(caught.exception.code, "dependencies.import-bom-mismatch")
        self.assertIn("requests", str(caught.exception))
        self.assertNotIn("main", str(caught.exception).split("BOM: ")[-1])

    def test_rust_crate_local_modules_are_not_external_dependencies(self) -> None:
        # `mod planner;` makes `use planner::...` a crate-local path. Treating it
        # as an external crate failed the whole lifecycle for a multi-module
        # generated Rust application that declared no dependencies at all.
        reconcile_generated_dependencies(
            {
                "source/main.rs": (
                    "mod planner;\n"
                    "pub mod tasks;\n"
                    "pub(crate) mod internal;\n"
                    "use planner::Schedule;\n"
                    "use tasks::Task;\n"
                    "use internal::helper;\n"
                    "use std::collections::HashMap;\n"
                    "fn main() {}\n"
                ),
                "source/planner.rs": "pub struct Schedule;\n",
                "source/tasks.rs": "pub struct Task;\n",
                "source/internal.rs": "pub fn helper() {}\n",
            },
            _inventory(),
        )

        with self.assertRaises(DependencyObservationError) as caught:
            reconcile_generated_dependencies(
                {
                    "source/main.rs": (
                        "mod planner;\n"
                        "use planner::Schedule;\n"
                        "use serde::Serialize;\n"
                        "fn main() {}\n"
                    ),
                    "source/planner.rs": "pub struct Schedule;\n",
                },
                _inventory(),
            )
        self.assertEqual(caught.exception.code, "dependencies.import-bom-mismatch")
        self.assertIn("serde", str(caught.exception))
        self.assertNotIn("planner", str(caught.exception))

    def test_rust_package_name_is_a_local_crate_import(self) -> None:
        reconcile_generated_dependencies(
            {
                "source/Cargo.toml": (
                    '[package]\nname = "tokenshark-policy"\nversion = "0.2.0"\n'
                ),
                "source/src/main.rs": (
                    "use tokenshark_policy::AppState;\nfn main() {}\n"
                ),
                "source/src/lib.rs": "pub struct AppState;\n",
            },
            _inventory(),
            allow_missing_cargo_lock=True,
        )

    def test_cpp_nested_test_can_include_generated_source_root_header(self) -> None:
        reconcile_generated_dependencies(
            {
                "source/app.hpp": "#pragma once\n",
                "source/app.cpp": '#include "app.hpp"\n',
                "source/tests/app_test.cpp": '#include "app.hpp"\nint main() {}\n',
            },
            _inventory(),
        )

    def test_native_inspector_parsers_preserve_exact_edges(self) -> None:
        uuids, dylibs = parse_dyld_info_summary(
            """sample [arm64]:
-uuid:
    AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE
-linked_dylibs:
    attributes     load path
                   @rpath/libportable.dylib
-rpaths:
                   /opt/portable/lib
"""
        )
        self.assertEqual(
            uuids,
            (("arm64", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),),
        )
        self.assertEqual(dylibs, ("@rpath/libportable.dylib",))
        _uuids, links = parse_dyld_info_links(
            """cache [arm64e]:
-linked_dylibs:
        weak-link     /usr/lib/liboptional.dylib
                      /usr/lib/librequired.dylib
                      @rpath/lib weird.dylib
"""
        )
        self.assertEqual(
            links,
            (
                ("/usr/lib/liboptional.dylib", True),
                ("/usr/lib/librequired.dylib", False),
                ("@rpath/lib weird.dylib", False),
            ),
        )
        self.assertEqual(
            parse_dyld_info_rpaths("cmd: LC_RPATH\npath: @loader_path/lib\n"),
            ("@loader_path/lib",),
        )
        self.assertEqual(
            parse_dyld_info_rpaths('cmd: LC_RPATH\nrpath:"@executable_path/../lib/"\n'),
            ("@executable_path/../lib/",),
        )
        self.assertEqual(
            parse_readelf_dynamic(
                """0x (NEEDED) Shared library: [libfirst.so]
0x (NEEDED) Shared library: [libsecond.so]
0x (RUNPATH) Library runpath: [$ORIGIN/lib:/opt/portable/lib]
"""
            ),
            (
                ("libfirst.so", "libsecond.so"),
                ("$ORIGIN/lib", "/opt/portable/lib"),
            ),
        )
        self.assertEqual(
            parse_ldconfig_cache(
                """2 libs found in cache `/etc/ld.so.cache'
 libportable.so (libc6,x86-64) => /lib/libportable.so
 libportable.so (libc6) => /lib32/libportable.so
"""
            ),
            {
                "libportable.so": (
                    "/lib/libportable.so",
                    "/lib32/libportable.so",
                )
            },
        )
        self.assertEqual(
            parse_dumpbin_dependents(
                """Image has the following dependencies:

    KERNEL32.dll
    portable.RUNTIME.DLL
"""
            ),
            ("kernel32.dll", "portable.runtime.dll"),
        )
        llvm_output = """Import {
  Name: KERNEL32.dll
}
DelayImport {
  Name: Optional.RUNTIME.dll
  Import {
    Symbol: optional (0)
  }
}
"""
        self.assertEqual(
            parse_llvm_readobj_imports(llvm_output),
            ("kernel32.dll", "optional.runtime.dll"),
        )
        self.assertEqual(
            _parse_llvm_readobj_import_records(llvm_output),
            (("kernel32.dll", False), ("optional.runtime.dll", True)),
        )
        self.assertEqual(
            _parse_dumpbin_import_records(
                """Image has the following dependencies:
    KERNEL32.dll
Image has the following delay load dependencies:
    Optional.RUNTIME.dll
"""
            ),
            (("kernel32.dll", False), ("optional.runtime.dll", True)),
        )

    def test_linux_ldconfig_is_exact_tool_evidence_not_an_elf_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact"
            artifact.mkdir()
            readelf = root / "readelf"
            readelf.write_bytes(b"\x7fELFfixture-readelf")
            ldconfig = root / "ldconfig"
            ldconfig.write_text('#!/bin/sh\nexec /sbin/ldconfig.real "$@"\n')
            observer = LinuxElfDependencyObserver(lifecycle_commands=())
            observed_seeds: dict[str, set[str]] = {}

            def closure(_readelf, seeds, loader_cache):
                observed_seeds.update(
                    (path, set(scopes)) for path, scopes in seeds.items()
                )
                self.assertEqual(
                    loader_cache, {"libfixture.so": ("/lib/libfixture.so",)}
                )
                return (
                    (
                        _ElfImage(
                            path=str(readelf.resolve()),
                            exact_identity=f"sha256:{'1' * 64}",
                            architecture=("ELF64", "fixture"),
                            needed=(),
                            search_paths=(),
                            interpreter=None,
                        ),
                    ),
                    (),
                )

            with (
                mock.patch(
                    "literate_ai.adapters.dependencies.observation.sys.platform",
                    "linux",
                ),
                mock.patch(
                    "literate_ai.adapters.dependencies.observation._resolved_bound_tool",
                    return_value=readelf.resolve(),
                ),
                mock.patch(
                    "literate_ai.adapters.dependencies.observation._find_linux_ldconfig",
                    return_value=ldconfig.resolve(),
                ),
                mock.patch(
                    "literate_ai.adapters.dependencies.observation._run_bounded_tool",
                    return_value=(
                        "1 libs found in cache `/etc/ld.so.cache'\n"
                        " libfixture.so (libc6,x86-64) => /lib/libfixture.so\n"
                    ),
                ),
                mock.patch.object(observer, "_closure", side_effect=closure),
            ):
                observation = observer.observe(
                    {"artifact_path": str(artifact)}, root_ref="urn:fixture:root"
                )

            self.assertIn(str(readelf.resolve()), observed_seeds)
            self.assertNotIn(str(ldconfig.resolve()), observed_seeds)
            tool = next(
                item
                for item in observation.components
                if item["name"] == "ldconfig-loader-cache-tool"
            )
            self.assertEqual(
                tool["hashes"],
                [
                    {
                        "alg": "SHA-256",
                        "content": _file_digest(ldconfig).removeprefix("sha256:"),
                    }
                ],
            )
            self.assertIn(("urn:fixture:root", tool["bom-ref"]), observation.edges)

    def test_windows_api_set_namespace_is_exact_and_host_independent(self) -> None:
        version, contracts, lookup_prefixes = _parse_windows_api_set_namespace(
            _api_set_namespace_fixture()
        )
        schema = _WindowsApiSetSchema(
            path=r"C:\Windows\System32\apisetschema.dll",
            digest=f"sha256:{'1' * 64}",
            namespace_version=version,
            contracts=contracts,
            lookup_prefixes=lookup_prefixes,
            mapping_identity=f"sha256:{'2' * 64}",
        )

        self.assertEqual(version, 6)
        self.assertEqual(
            schema.resolve(
                "api-ms-win-core-fixture-l1-1-0.dll",
                importer=r"C:\fixture\ordinary.dll",
            ).host,
            "kernelbase.dll",
        )
        self.assertEqual(
            schema.resolve(
                "api-ms-win-core-fixture-l1-1-0.dll",
                importer=r"C:\fixture\special.dll",
            ).host,
            "specialhost.dll",
        )
        compatible = schema.resolve(
            "api-ms-win-core-fixture-l1-1-9.dll",
            importer=r"C:\fixture\ordinary.dll",
        )
        self.assertEqual(
            compatible.schema_contract,
            "api-ms-win-core-fixture-l1-1-0.dll",
        )

    def test_windows_inspector_prefers_dumpbin_then_falls_back_to_llvm(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tool = Path(temporary) / "inspector.exe"
            tool.write_bytes(b"exact-inspector")

            with (
                mock.patch(
                    "literate_ai.adapters.dependencies.observation.shutil.which",
                    return_value=str(tool),
                ),
                mock.patch(
                    "literate_ai.adapters.dependencies.observation._run_bounded_tool",
                    return_value=("Microsoft (R) COFF/PE Dumper Version 14.44.35211.0"),
                ),
            ):
                dumpbin = _find_windows_pe_inspector()
            self.assertEqual(
                (dumpbin.kind, dumpbin.version), ("dumpbin", "14.44.35211.0")
            )

            def llvm_only(command: str) -> str | None:
                return None if command == "dumpbin" else str(tool)

            with (
                mock.patch(
                    "literate_ai.adapters.dependencies.observation.shutil.which",
                    side_effect=llvm_only,
                ),
                mock.patch(
                    "literate_ai.adapters.dependencies.observation._run_bounded_tool",
                    return_value="LLVM version 22.1.7\n",
                ),
            ):
                llvm = _find_windows_pe_inspector()
            self.assertEqual((llvm.kind, llvm.version), ("llvm-readobj", "22.1.7"))
            self.assertEqual(llvm.digest, _file_digest(tool.resolve()))

    def test_dumpbin_usage_status_is_admitted_only_for_version_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tool = Path(temporary).resolve() / "dumpbin.exe"
            tool.write_bytes(b"exact-dumpbin")
            completed = SimpleNamespace(
                returncode=1100,
                stdout=b"Microsoft COFF/PE Dumper Version 14.44.35228.0\n",
                stderr=b"usage: DUMPBIN [options] [files]\n",
            )
            with mock.patch.object(
                dependency_lifecycle.subprocess,
                "run",
                return_value=completed,
            ):
                output = dependency_lifecycle._run_bounded_tool(
                    tool,
                    ("/?",),
                    code="dependencies.dumpbin-version-failed",
                    allowed_returncodes=frozenset({0, 1100}),
                )
                with self.assertRaises(DependencyObservationError):
                    dependency_lifecycle._run_bounded_tool(
                        tool,
                        ("/?",),
                        code="dependencies.dumpbin-image-failed",
                    )

        self.assertIn("Version 14.44.35228.0", output)

    def test_pe_closure_binds_virtual_contracts_and_rejects_missing_dlls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            application = root / "application.exe"
            host = root / "kernelbase.dll"
            schema_path = root / "apisetschema.dll"
            inspector_path = root / "llvm-readobj.exe"
            for path in (application, host, schema_path, inspector_path):
                _minimal_pe(path)
            contract = "api-ms-win-core-fixture-l1-1-0.dll"
            schema = _WindowsApiSetSchema(
                path=str(schema_path.resolve()),
                digest=_file_digest(schema_path.resolve()),
                namespace_version=6,
                contracts=((contract, (("", host.name.casefold()),)),),
                lookup_prefixes=((contract, contract.removesuffix("-0.dll")),),
                mapping_identity=f"sha256:{'3' * 64}",
            )
            inspector = _PeInspector(
                path=inspector_path.resolve(),
                kind="llvm-readobj",
                version="22.1.7",
                version_output_identity=f"sha256:{'4' * 64}",
                digest=_file_digest(inspector_path.resolve()),
            )

            def imports(
                _inspector: _PeInspector, path: Path
            ) -> tuple[tuple[str, bool], ...]:
                if path == application.resolve():
                    return ((contract, False),)
                return ()

            with mock.patch(
                "literate_ai.adapters.dependencies.observation._inspect_pe_imports",
                side_effect=imports,
            ):
                images, edges, bindings, unavailable = _pe_closure(
                    inspector,
                    {
                        str(application.resolve()): {"runtime"},
                        str(schema_path.resolve()): {"system"},
                    },
                    (root,),
                    api_set_schema=schema,
                )
            self.assertEqual(len(images), 3)
            self.assertEqual(edges, ())
            self.assertEqual(unavailable, ())
            self.assertEqual(
                bindings[0].target_path,
                str(host.resolve()),
            )

            with mock.patch(
                "literate_ai.adapters.dependencies.observation._inspect_pe_imports",
                return_value=(("required-missing.dll", False),),
            ):
                with self.assertRaises(DependencyObservationError) as caught:
                    _pe_closure(
                        inspector,
                        {str(application.resolve()): {"runtime"}},
                        (root,),
                        api_set_schema=schema,
                    )
            self.assertEqual(caught.exception.code, "dependencies.pe-import-unresolved")

            with mock.patch(
                "literate_ai.adapters.dependencies.observation._inspect_pe_imports",
                return_value=(("optional-missing.dll", True),),
            ):
                _images, _edges, _bindings, unavailable = _pe_closure(
                    inspector,
                    {str(application.resolve()): {"runtime"}},
                    (root,),
                    api_set_schema=schema,
                )
            self.assertEqual(unavailable[0].name, "optional-missing.dll")

    def test_pe_detection_reads_only_the_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "large.dll"
            offset = 0x80
            content = bytearray(8 * 1024 * 1024)
            content[:2] = b"MZ"
            content[0x3C:0x40] = offset.to_bytes(4, "little")
            content[offset : offset + 4] = b"PE\x00\x00"
            image.write_bytes(content)
            requested: list[int] = []
            real_read = os.read

            def record_read(descriptor: int, size: int) -> bytes:
                requested.append(size)
                return real_read(descriptor, size)

            with mock.patch(
                "literate_ai.adapters.dependencies.observation.os.read",
                side_effect=record_read,
            ):
                self.assertTrue(_is_pe(image))

            self.assertEqual(requested, [0x40, 4])

    def test_pe_import_resolution_reuses_exact_candidate_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = root / "fixture.dll"
            _minimal_pe(image)
            directory_entries: dict[Path, dict[str, Path]] = {}
            validity: dict[Path, bool] = {}

            with mock.patch(
                "literate_ai.adapters.dependencies.observation._is_pe",
                wraps=_is_pe,
            ) as check:
                first = _resolve_pe_import(
                    image.name,
                    (root,),
                    directory_entries=directory_entries,
                    pe_validity=validity,
                )
                second = _resolve_pe_import(
                    image.name,
                    (root,),
                    directory_entries=directory_entries,
                    pe_validity=validity,
                )

            self.assertEqual(first, second)
            self.assertEqual(check.call_count, 1)

    def test_absent_weak_macho_link_is_optional_but_present_link_is_retained(
        self,
    ) -> None:
        observer = _FixtureMacObserver({"/usr/lib/libpresent.dylib"})
        arguments = {
            "loader_dir": "/fixture",
            "executable_dir": "/fixture",
            "rpaths": (),
        }
        self.assertIsNone(
            observer._resolve_load_path(
                "/usr/lib/libabsent.dylib", weak=True, **arguments
            )
        )
        self.assertEqual(
            observer._resolve_load_path(
                "/usr/lib/libpresent.dylib", weak=True, **arguments
            ),
            "/usr/lib/libpresent.dylib",
        )
        with self.assertRaises(DependencyObservationError):
            observer._resolve_load_path(
                "/usr/lib/libabsent.dylib", weak=False, **arguments
            )

    def test_macho_rpath_stack_is_inherited_by_transitive_images(self) -> None:
        target = "/fixture/app/lib/lib weird.dylib"
        observer = _FixtureMacObserver({target})
        parent_rpaths = observer._expanded_rpaths(
            ("@loader_path/lib",),
            loader_dir="/fixture/app",
            executable_dir="/fixture/app",
            inherited_rpaths=(),
        )
        child_rpaths = observer._expanded_rpaths(
            (),
            loader_dir="/fixture/frameworks",
            executable_dir="/fixture/app",
            inherited_rpaths=parent_rpaths,
        )

        self.assertEqual(parent_rpaths, ("/fixture/app/lib",))
        self.assertEqual(child_rpaths, parent_rpaths)
        self.assertNotIn("\\", parent_rpaths[0])
        self.assertEqual(
            observer._resolve_load_path(
                "@rpath/lib weird.dylib",
                loader_dir="/fixture/frameworks",
                executable_dir="/fixture/app",
                rpaths=child_rpaths,
            ),
            target,
        )

    def test_combined_macho_inspection_preserves_all_image_facts(self) -> None:
        summary = """fixture [arm64]:
-uuid:
    AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE
-linked_dylibs:
    attributes     load path
    weak-link      /usr/lib/liboptional.dylib
                   @rpath/librequired.dylib
-load_commands:
Load command #0
    cmd: LC_RPATH
    rpath: "@loader_path/lib/"
Load command #1
    cmd: LC_ID_DYLIB
    path: /fixture/libself.dylib
fixture [x86_64]:
-uuid:
    BBBBBBBB-CCCC-DDDD-EEEE-FFFFFFFFFFFF
-linked_dylibs:
    attributes     load path
                   /usr/lib/liboptional.dylib
                   @rpath/librequired.dylib
-load_commands:
Load command #0
    cmd: LC_RPATH
    path: @executable_path/../lib
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.dylib"
            path.write_bytes(b"fixture-mach-o")
            observer = MacOsMachODependencyObserver(lifecycle_commands=())
            with mock.patch.object(observer, "_run_dyld", return_value=summary):
                image = observer._inspect(str(path))
            self.assertEqual(
                image.uuids,
                (
                    ("arm64", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
                    ("x86_64", "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"),
                ),
            )
            self.assertEqual(
                image.linked_paths,
                (
                    ("/usr/lib/liboptional.dylib", False),
                    ("@rpath/librequired.dylib", False),
                ),
            )
            self.assertEqual(
                image.rpaths,
                ("@executable_path/../lib", "@loader_path/lib/"),
            )
            self.assertIsNotNone(image.materialized_file)

    def test_materialized_macho_symlink_chain_is_bound_into_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "libfixture.1.2.3.dylib"
            target.write_bytes(b"fixture-mach-o")
            link = root / "libfixture.1.dylib"
            try:
                link.symlink_to(target.name)
            except OSError:
                self.skipTest("host cannot create file symlinks")

            summary = """fixture [arm64]:
-uuid:
    AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE
-linked_dylibs:
    attributes     load path
"""

            def inspect(arguments):
                if arguments[0] == "-uuid":
                    return summary
                if arguments[0] == "-load_commands":
                    return ""
                self.fail(f"unexpected dyld_info arguments: {arguments!r}")

            observer = MacOsMachODependencyObserver(lifecycle_commands=())
            with mock.patch.object(observer, "_run_dyld", side_effect=inspect):
                image = observer._inspect(str(link))

            self.assertIsNotNone(image.materialized_file)
            assert image.materialized_file is not None
            self.assertEqual(
                image.materialized_file.resolved_path, str(target.resolve(strict=True))
            )
            self.assertEqual(
                image.materialized_file.content_identity,
                "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
            )
            component = _macho_component(image, _macho_ref(image), scopes=("runtime",))
            properties = {
                (item["name"], item["value"]) for item in component["properties"]
            }
            self.assertIn(
                (
                    "literate-ai:macho-resolved-path",
                    str(target.resolve(strict=True)),
                ),
                properties,
            )
            self.assertIn(
                (
                    "literate-ai:macho-symlink",
                    json.dumps(
                        {"path": str(link), "target": target.name},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
                properties,
            )

    def test_macho_symlink_retarget_during_inspection_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = root / "before.dylib"
            after = root / "after.dylib"
            before.write_bytes(b"before")
            after.write_bytes(b"after")
            link = root / "current.dylib"
            try:
                link.symlink_to(before.name)
            except OSError:
                self.skipTest("host cannot create file symlinks")

            summary = """fixture [arm64]:
-uuid:
    AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE
-linked_dylibs:
    attributes     load path
"""

            def inspect(arguments):
                if arguments[0] == "-uuid":
                    link.unlink()
                    link.symlink_to(after.name)
                    return summary
                if arguments[0] == "-load_commands":
                    return ""
                self.fail(f"unexpected dyld_info arguments: {arguments!r}")

            observer = MacOsMachODependencyObserver(lifecycle_commands=())
            with (
                mock.patch.object(observer, "_run_dyld", side_effect=inspect),
                self.assertRaises(DependencyObservationError) as caught,
            ):
                observer._inspect(str(link))
            self.assertEqual(caught.exception.code, "dependencies.macos-image-changed")

    def test_dyld_shared_cache_only_symlink_uses_uuid_without_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            link = root / "SharedCache.framework"
            try:
                link.symlink_to("Versions/A/SharedCache")
            except OSError:
                self.skipTest("host cannot create file symlinks")

            summary = """fixture [arm64e]:
-uuid:
    AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE
-linked_dylibs:
    attributes     load path
"""

            def inspect(arguments):
                if arguments[0] == "-uuid":
                    return summary
                if arguments[0] == "-load_commands":
                    return ""
                self.fail(f"unexpected dyld_info arguments: {arguments!r}")

            observer = MacOsMachODependencyObserver(lifecycle_commands=())
            with mock.patch.object(observer, "_run_dyld", side_effect=inspect):
                image = observer._inspect(str(link))

            self.assertIsNone(image.materialized_file)
            component = _macho_component(image, _macho_ref(image), scopes=("runtime",))
            self.assertNotIn("hashes", component)
            self.assertIn(
                {
                    "name": "literate-ai:macho-uuid:arm64e",
                    "value": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                },
                component["properties"],
            )

    def test_file_evidence_rejects_symlinks_and_mid_read_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.bin"
            target.write_bytes(b"a" * (2 * 1024 * 1024))
            real_read = os.read
            changed = False

            def mutate_after_read(descriptor: int, size: int) -> bytes:
                nonlocal changed
                content = real_read(descriptor, size)
                if content and not changed:
                    changed = True
                    target.write_bytes(b"changed")
                return content

            with mock.patch(
                "literate_ai.adapters.dependencies.observation.os.read",
                side_effect=mutate_after_read,
            ):
                with self.assertRaises(DependencyObservationError) as caught:
                    _stable_file_bytes(target)
            self.assertEqual(caught.exception.code, "dependencies.file-changed")

            target.write_bytes(b"stable")
            real_fstat = os.fstat

            def fstat_with_distinct_ctime(descriptor: int) -> SimpleNamespace:
                observed = real_fstat(descriptor)
                return SimpleNamespace(
                    st_mode=observed.st_mode,
                    st_dev=observed.st_dev,
                    st_ino=observed.st_ino,
                    st_size=observed.st_size,
                    st_mtime_ns=observed.st_mtime_ns,
                    st_ctime_ns=observed.st_ctime_ns + 1,
                )

            with mock.patch(
                "literate_ai.adapters.dependencies.observation.os.fstat",
                side_effect=fstat_with_distinct_ctime,
            ):
                self.assertEqual(_stable_file_bytes(target), b"stable")

            link = root / "link.bin"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("host cannot create file symlinks")
            with self.assertRaises(DependencyObservationError):
                _file_digest(link)


if __name__ == "__main__":
    unittest.main()
