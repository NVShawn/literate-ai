"""Shared test fixtures extracted from test_standard_npm_lifecycle."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.builders import BoundedProcessResult
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    StandardNpmLifecycleError,
    StandardNpmTarget,
    local_generated_source_tree_identity,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import _local_tree_identity
from literate_ai.adapters.lifecycle.standard_npm import (
    StandardNpmLockedPackage,
    StandardNpmSourceAuthority,
    load_npm_source_authority,
    parse_npm_source_authority,
    validate_npm_inventory,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    verify_qualification_build,
)
from literate_ai.contracts import (
    BuildPrivilege,
    BuildSubActionKind,
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentLifecycleCommand,
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from tests.support.fixtures_test_component_node_generation_preparation import (
    _fixture as _generation_fixture,
)
from tests.support.fixtures_test_standard_local_command_adapter import (
    copy_digest_cache_without_sidecars,
)
from tests.unit.standard_source_evidence_fixture import register_strict_source

_PACKAGE_NAME = "is-number"

_PACKAGE_VERSION = "7.0.0"

_PACKAGE_REF = f"pkg:npm/{_PACKAGE_NAME}"


def _identity(label: str):
    return canonical_identity({"standard-npm-lifecycle-test": label})


def _package_component(
    *, version_range: str = "vers:npm/>=7.0.0|<8.0.0"
) -> dict[str, object]:
    return {
        "type": "library",
        "bom-ref": _PACKAGE_REF,
        "name": _PACKAGE_NAME,
        "purl": _PACKAGE_REF,
        "isExternal": True,
        "versionRange": version_range,
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "package"},
            {"name": "literate-ai:dependency-scope", "value": "runtime"},
        ],
    }


def _package_manifest() -> bytes:
    return json.dumps(
        {
            "name": "fixture-app",
            "version": "1.0.0",
            "type": "commonjs",
            "dependencies": {_PACKAGE_NAME: _PACKAGE_VERSION},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _package_lock(*, resolved: str | None = None) -> bytes:
    integrity = "sha256-" + base64.b64encode(b"a" * 32).decode()
    dependency: dict[str, object] = {
        "version": _PACKAGE_VERSION,
        "resolved": resolved
        or ("https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz"),
        "integrity": integrity,
    }
    return json.dumps(
        {
            "name": "fixture-app",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": "fixture-app",
                    "version": "1.0.0",
                    "dependencies": {_PACKAGE_NAME: _PACKAGE_VERSION},
                },
                f"node_modules/{_PACKAGE_NAME}": dependency,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _npm_inventory() -> bytes:
    return json.dumps(
        {
            "name": "fixture-app",
            "version": "1.0.0",
            "dependencies": {
                _PACKAGE_NAME: {
                    "version": _PACKAGE_VERSION,
                    "dependencies": {},
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class StandardNpmLifecycleTests(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        selected: bool = True,
        lockfile: bool = True,
        source_bom_dependency: bool = True,
        source_bom_version_range: str = "vers:npm/>=7.0.0|<8.0.0",
        npmrc: bool = False,
        javascript_files: tuple[str, ...] = ("main.js",),
    ):
        snapshot, execution = _generation_fixture()
        generation_plan = execution.generation_plans[0]
        node_binding = LocalComponentToolBinding(sys.executable, ("-I",))
        npm_binding = LocalComponentToolBinding(sys.executable, ("-E",))
        target = StandardNpmTarget(
            generation_plan.component_revision,
            _identity("package-npm-flavor"),
            _identity("package-npm-profile"),
            _identity("npm-resolver"),
            npm_binding.toolchain_identity,
            node_binding.toolchain_identity,
            "source/package.json",
            "source/package-lock.json",
            npm_binding.command,
        )
        commands = (
            ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                (
                    "{tool}",
                    "ci",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--no-bin-links",
                    "{source_root}",
                    "{object_root}",
                    "{export_path}",
                ),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.TEST,
                ("{tool}", "{artifact_root}", "--litai-test"),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.EXECUTE,
                ("{tool}", "{artifact_root}", "--litai-smoke"),
            ),
        )
        contract = ComponentCommandContract(
            component_revision=generation_plan.component_revision,
            locked_build_authority_identity=target.identity,
            build_system_resolver_identity=target.build_system_resolver_identity,
            build_system_toolchain_identity=target.build_system_toolchain_identity,
            language_compiler_identity=target.node_toolchain_identity,
            language_runtime_identity=target.node_toolchain_identity,
            commands=commands,
            tool_bindings=(
                ComponentCommandToolBinding(
                    ComponentCommandPhase.BUILD, npm_binding.toolchain_identity
                ),
                ComponentCommandToolBinding(
                    ComponentCommandPhase.TEST, node_binding.toolchain_identity
                ),
                ComponentCommandToolBinding(
                    ComponentCommandPhase.EXECUTE, node_binding.toolchain_identity
                ),
            ),
            artifact_export=ComponentArtifactExportShape(
                "app",
                "portable-application",
                _identity("abi"),
                _identity("target"),
                "application/vnd.literate-ai.directory",
                _identity("producer"),
            ),
        )
        source = root / "generated"
        package_root = source / "source"
        package_root.mkdir(parents=True)
        (package_root / "package.json").write_bytes(_package_manifest())
        if lockfile:
            (package_root / "package-lock.json").write_bytes(_package_lock())
        for relative in javascript_files:
            path = package_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "const value = require('is-number');\n"
                "console.log(JSON.stringify({component: String(value(7))}));\n",
                encoding="utf-8",
            )
        if npmrc:
            (package_root / ".npmrc").write_text(
                "//registry.npmjs.org/:_authToken=forbidden\n", encoding="utf-8"
            )
        registry = LocalSourceTreeRegistry()
        candidate = register_strict_source(
            registry,
            source,
            snapshot=snapshot,
            generation_plan=generation_plan,
            identity_namespace="standard-npm-lifecycle-test",
            additional_components=(
                (_package_component(version_range=source_bom_version_range),)
                if source_bom_dependency
                else ()
            ),
            root_dependency_refs=((_PACKAGE_REF,) if source_bom_dependency else ()),
        )
        ports = LocalStandardLifecyclePorts(
            source_trees=registry,
            object_root=root / "objects",
            contracts=(contract,),
            tool_bindings=(npm_binding, node_binding),
            npm_targets=((target,) if selected else ()),
        )
        intent = ports.create(execution, generation_plan, candidate, (), ())
        authorization = ports.authorize(
            intent,
            ports.index(candidate.component_revision, candidate.tree_identity),
        )
        plan = ports.finalize(intent, authorization)
        return ports, plan, source, candidate, target

    def test_constructor_rejects_foreign_locked_npm_build_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, _plan, _source, _candidate, target = self._fixture(root)
            contract = next(iter(ports.contracts.values()))
            with self.assertRaisesRegex(
                ValueError, "does not match locked command authority"
            ):
                LocalStandardLifecyclePorts(
                    source_trees=ports.source_trees,
                    object_root=root / "foreign-authority-objects",
                    contracts=(
                        replace(
                            contract,
                            locked_build_authority_identity=_identity(
                                "foreign-build-authority"
                            ),
                        ),
                    ),
                    tool_bindings=tuple(ports.tool_bindings.values()),
                    npm_targets=(target,),
                )

    @staticmethod
    def _successful_npm(calls: list[dict[str, object]]):
        def invoke(command, **kwargs):
            call = {"command": tuple(command), **kwargs}
            calls.append(call)
            if "ci" in command:
                installed = kwargs["cwd"] / "node_modules" / _PACKAGE_NAME
                installed.mkdir(parents=True)
                (installed / "index.js").write_text(
                    "module.exports = value => typeof value === 'number';\n",
                    encoding="utf-8",
                )
                return BoundedProcessResult(0, b"", b"")
            return BoundedProcessResult(0, _npm_inventory(), b"")

        return invoke

    def test_selected_target_resolves_before_compile_and_retains_runtime_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, source, candidate, npm_target = self._fixture(root)
            recorder = QualificationEvidenceRecorder(
                max_bytes=5_000_000, max_records=1000
            )
            ports.retain_evidence_with(recorder)
            before = local_generated_source_tree_identity(source)
            calls: list[dict[str, object]] = []
            ambient = {
                "NPM_CONFIG_REGISTRY": "https://credentials.invalid/",
                "npm_token": "secret-one",
                "NODE_AUTH_TOKEN": "secret-two",
                "NODE_OPTIONS": "--require=/tmp/ambient.js",
                "NODE_PATH": "/tmp/ambient-modules",
                "Node_Compile_Cache": "/tmp/ambient-compile-cache",
                "node_disable_compile_cache": "0",
            }
            with (
                mock.patch.dict(os.environ, ambient, clear=False),
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=self._successful_npm(calls),
                ),
                mock.patch.object(
                    ports,
                    "_run",
                    side_effect=lambda argv, **_kwargs: subprocess.CompletedProcess(
                        argv,
                        0,
                        (
                            json.dumps(
                                {
                                    "schema": "literate-ai/generated-test-results@1",
                                    "cases": [
                                        {
                                            "case_id": f"fixture-{category}",
                                            "outcome": "passed",
                                        }
                                        for category in (
                                            "example",
                                            "boundary",
                                            "invariant",
                                        )
                                    ],
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            if "--litai-test" in argv
                            else (
                                '{"component":"application"}'
                                if "--litai-smoke" in argv
                                else ""
                            )
                        ),
                        "",
                    ),
                ) as node_process,
            ):
                output = ports.build(plan, ())
                test_evidence = ports.test(plan, output.exports)
                execution_evidence = ports.execute(plan, output.exports)
                cached = ports.build(plan, ())

            self.assertEqual(
                tuple(item.kind for item in plan.request.sub_actions),
                (
                    BuildSubActionKind.RESOLVE_DEPENDENCIES,
                    BuildSubActionKind.COMPILE,
                ),
            )
            self.assertEqual(
                plan.request.requested_privileges,
                (
                    BuildPrivilege.EXECUTE_BUILD_TOOLS,
                    BuildPrivilege.NETWORK_ACCESS,
                ),
            )
            self.assertEqual(len(plan.manifest.actions), 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(output, cached)
            self.assertEqual(len(test_evidence.cases), 3)
            self.assertEqual(
                ports.execution_stdout[candidate.component_revision.uri],
                '{"component":"application"}',
            )
            self.assertNotEqual(test_evidence.identity, execution_evidence.identity)
            self.assertEqual(ports.build_cache_misses, 1)
            self.assertEqual(ports.build_cache_hits, 1)
            self.assertEqual(
                calls[0]["command"],
                (
                    *npm_target.npm_command,
                    "ci",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--no-bin-links",
                ),
            )
            self.assertEqual(
                calls[1]["command"],
                (*npm_target.npm_command, "ls", "--all", "--json"),
            )
            for call in calls:
                environment = call["environment"]
                self.assertTrue(
                    all(
                        name.casefold()
                        not in {
                            "node_auth_token",
                            "node_options",
                            "node_path",
                            "node_compile_cache",
                            "npm_token",
                            "npm_config_registry",
                        }
                        for name in environment
                    )
                )
                self.assertEqual(environment["NODE_DISABLE_COMPILE_CACHE"], "1")
                self.assertTrue(
                    Path(environment["NPM_CONFIG_CACHE"]).is_relative_to(
                        ports.object_root
                    )
                )
                self.assertTrue(
                    Path(environment["NPM_CONFIG_USERCONFIG"]).is_relative_to(
                        ports.object_root
                    )
                )
            artifact = ports.artifact_path(output.exports[0])
            self.assertTrue(
                (
                    artifact / "source" / "node_modules" / _PACKAGE_NAME / "index.js"
                ).is_file()
            )
            evidence = json.loads(
                (artifact.parent / ".literate/npm/evidence-manifest.json").read_bytes()
            )
            self.assertEqual(
                evidence["packaging_flavor_revision_identity"],
                npm_target.packaging_flavor_revision_identity.uri,
            )
            self.assertEqual(
                evidence["npm_toolchain_identity"],
                npm_target.build_system_toolchain_identity.uri,
            )
            self.assertEqual(
                evidence["node_toolchain_identity"],
                npm_target.node_toolchain_identity.uri,
            )
            self.assertEqual(
                local_generated_source_tree_identity(source), candidate.tree_identity
            )
            self.assertEqual(local_generated_source_tree_identity(source), before)
            self.assertFalse((source / "source/node_modules").exists())
            self.assertEqual(
                sum("--check" in call.args[0] for call in node_process.call_args_list),
                1,
            )
            syntax_argv = next(
                call.args[0]
                for call in node_process.call_args_list
                if "--check" in call.args[0]
            )
            self.assertTrue(Path(syntax_argv[-1]).is_relative_to(ports.object_root))
            self.assertFalse(Path(syntax_argv[-1]).is_relative_to(source))

            retained_files = tuple(
                (
                    ContentIdentity.parse_uri(item["identity"]),
                    (artifact.parent / item["path"]).read_bytes(),
                )
                for item in evidence["files"]
            )
            source_authority = load_npm_source_authority(source, npm_target)
            build_identity = output.evidence.build_observation_identity

        # The temporary source, npm installation and artifact custody are gone.
        self.assertFalse(root.exists())
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        verify_qualification_build(reader, plan=plan, build=output.evidence)
        build = reader.read_json(build_identity)
        process = reader.read_json(
            ContentIdentity.parse_uri(build["process_observation_identity"])
        )
        self.assertEqual(process["schema"], "literate-ai/local-npm-build-observation@1")
        self.assertEqual(
            reader.read_json(
                ContentIdentity.parse_uri(process["dependency_evidence_manifest"])
            ),
            evidence,
        )
        self.assertEqual(
            reader.read_json(npm_target.identity), npm_target.identity_document()
        )
        self.assertEqual(
            reader.read_json(source_authority.identity),
            source_authority.identity_document(),
        )
        reopened = parse_npm_source_authority(
            reader.read_bytes(source_authority.manifest_identity),
            reader.read_bytes(source_authority.lockfile_identity),
            npm_target,
        )
        self.assertEqual(reopened, source_authority)
        for field, phase in (
            ("install_process_identity", "npm-ci"),
            ("inventory_process_identity", "npm-ls"),
        ):
            observation = reader.read_json(ContentIdentity.parse_uri(evidence[field]))
            self.assertEqual(
                observation["schema"], "literate-ai/local-npm-process-observation@1"
            )
            self.assertEqual(observation["phase"], phase)
            self.assertEqual(observation["plan_identity"], plan.identity.uri)
            self.assertEqual(observation["returncode"], 0)
        for identity, content in retained_files:
            self.assertEqual(reader.read_bytes(identity), content)
        for identity in process["syntax_checks"]:
            syntax = reader.read_json(ContentIdentity.parse_uri(identity))
            self.assertEqual(syntax["phase"], "node-check")
            self.assertEqual(syntax["returncode"], 0)
            self.assertEqual(
                reader.read_json(ContentIdentity.parse_uri(syntax["stdout_identity"])),
                "",
            )
            self.assertEqual(
                reader.read_json(ContentIdentity.parse_uri(syntax["stderr_identity"])),
                "",
            )

        required = (
            build_identity,
            ContentIdentity.parse_uri(build["process_observation_identity"]),
            npm_target.identity,
            source_authority.identity,
            source_authority.manifest_identity,
            source_authority.lockfile_identity,
            ContentIdentity.parse_uri(process["dependency_evidence_manifest"]),
            ContentIdentity.parse_uri(evidence["inventory_identity"]),
            ContentIdentity.parse_uri(evidence["install_process_identity"]),
            ContentIdentity.parse_uri(evidence["inventory_process_identity"]),
            *(ContentIdentity.parse_uri(item) for item in process["syntax_checks"]),
        )
        for missing in required:
            incomplete = QualificationEvidenceReader(
                tuple(item for item in recorder.entries if item[0] != missing),
                max_bytes=5_000_000,
                max_records=1000,
            )
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                verify_qualification_build(incomplete, plan=plan, build=output.evidence)
        install = reader.read_json(
            ContentIdentity.parse_uri(evidence["install_process_identity"])
        )
        false_install = recorder.remember_json({**install, "returncode": False})
        for changes in (
            {"source_tree_identity": _identity("foreign-source").uri},
            {"files": evidence["files"][:-1]},
            {"install_process_identity": false_install.uri},
            {"accepted": True},
        ):
            altered_dependency = recorder.remember_json({**evidence, **changes})
            altered_process = recorder.remember_json(
                {**process, "dependency_evidence_manifest": altered_dependency.uri}
            )
            altered_build = recorder.remember_json(
                {**build, "process_observation_identity": altered_process.uri}
            )
            reopened = QualificationEvidenceReader(
                recorder.entries, max_bytes=5_000_000, max_records=1000
            )
            with (
                self.subTest(changes=changes),
                self.assertRaises(QualificationCaptureError),
            ):
                verify_qualification_build(
                    reopened,
                    plan=plan,
                    build=replace(
                        output.evidence, build_observation_identity=altered_build
                    ),
                )

    def test_retained_npm_parser_rejects_invalid_graphs_and_bounds_before_decode(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, source, _, target = self._fixture(Path(temporary))
            manifest = source.joinpath(target.manifest).read_bytes()
            lockfile = source.joinpath(target.lockfile).read_bytes()
        for bad_manifest, bad_lock in (
            (bytearray(manifest), lockfile),
            (manifest, b"[]"),
        ):
            with (
                self.subTest(content=type(bad_manifest)),
                self.assertRaises(StandardNpmLifecycleError),
            ):
                parse_npm_source_authority(bad_manifest, bad_lock, target)
        changed = json.loads(lockfile)
        changed["packages"]["node_modules/is-number"]["version"] = "8.0.0"
        with self.assertRaises(StandardNpmLifecycleError):
            parse_npm_source_authority(manifest, json.dumps(changed).encode(), target)
        with (
            mock.patch(
                "literate_ai.adapters.lifecycle.standard_npm._MAX_NPM_DOCUMENT_BYTES", 1
            ),
            mock.patch(
                "literate_ai.adapters.lifecycle.standard_npm._json_document"
            ) as decode,
            self.assertRaises(StandardNpmLifecycleError),
        ):
            parse_npm_source_authority(manifest, lockfile, target)
        decode.assert_not_called()

    def test_dependency_manifest_without_package_flavor_fails_before_any_tool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, _source, _candidate, _target = self._fixture(
                Path(temporary), selected=False
            )
            self.assertEqual(ports.npm_targets, {})
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process"
                ) as npm,
                mock.patch.object(ports, "_run") as generic,
                self.assertRaisesRegex(
                    LocalStandardLifecycleError,
                    "require an explicit package-npm target",
                ),
            ):
                ports.build(plan, ())
            npm.assert_not_called()
            generic.assert_not_called()

    def test_missing_lock_and_mismatched_bom_fail_before_npm(self) -> None:
        for case, options, message in (
            (
                "missing-lock",
                {"lockfile": False},
                "package-lock.json is not a regular file",
            ),
            (
                "mismatched-bom",
                {"source_bom_dependency": False},
                "source dependency authority is inconsistent",
            ),
            (
                "project-npmrc",
                {"npmrc": True},
                "unauthorized project configuration",
            ),
            (
                "mismatched-bom-range",
                {"source_bom_version_range": "vers:npm/>=8.0.0|<9.0.0"},
                "has no exact lock or observation",
            ),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                with (
                    mock.patch(
                        "literate_ai.adapters.lifecycle.standard_local.run_bounded_process"
                    ) as npm,
                    self.assertRaisesRegex(LocalStandardLifecycleError, message),
                ):
                    self._fixture(Path(temporary), **options)
                npm.assert_not_called()

    def test_failed_install_deletes_staging_and_never_mutates_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, source, candidate, _closure = self._fixture(root)
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    return_value=BoundedProcessResult(1, b"", b"network failed"),
                ),
                self.assertRaisesRegex(LocalStandardLifecycleError, "network failed"),
            ):
                ports.build(plan, ())

            self.assertEqual(list(ports.object_root.iterdir()), [])
            self.assertEqual(
                local_generated_source_tree_identity(source), candidate.tree_identity
            )

    def test_npm_component_with_only_an_mjs_source_builds_and_checks_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, _source, _candidate, _target = self._fixture(
                Path(temporary), javascript_files=("main.mjs",)
            )
            calls: list[dict[str, object]] = []
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=self._successful_npm(calls),
                ),
                mock.patch.object(
                    ports,
                    "_run",
                    return_value=subprocess.CompletedProcess((), 0, "", ""),
                ) as node_process,
            ):
                ports.build(plan, ())

            checked = tuple(
                Path(call.args[0][-1]).name
                for call in node_process.call_args_list
                if "--check" in call.args[0]
            )
            self.assertEqual(checked, ("main.mjs",))

    def test_npm_build_syntax_checks_every_javascript_module_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, _source, _candidate, _target = self._fixture(
                Path(temporary),
                javascript_files=(
                    "main.js",
                    "lib/common.cjs",
                    "lib/module.mjs",
                    "lib/metadata.json",
                ),
            )
            calls: list[dict[str, object]] = []
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=self._successful_npm(calls),
                ),
                mock.patch.object(
                    ports,
                    "_run",
                    return_value=subprocess.CompletedProcess((), 0, "", ""),
                ) as node_process,
            ):
                ports.build(plan, ())

            checked = tuple(
                Path(call.args[0][-1]).relative_to(ports.object_root).parts[-2:]
                for call in node_process.call_args_list
                if "--check" in call.args[0]
            )
            self.assertEqual(
                checked,
                (("lib", "common.cjs"), ("lib", "module.mjs"), ("source", "main.js")),
            )

    def test_real_v3_lock_shape_infers_name_and_rejects_nonregistry_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ports, _plan, source, _candidate, target = self._fixture(root)
            authority = load_npm_source_authority(source, target)
            self.assertEqual(authority.packages[0].name, _PACKAGE_NAME)

            (source / "source/package-lock.json").write_bytes(
                _package_lock(resolved="git+https://example.invalid/package.git")
            )
            with self.assertRaisesRegex(
                StandardNpmLifecycleError, "canonical registry.npmjs.org archive"
            ):
                load_npm_source_authority(source, target)

            native_lock = json.loads(_package_lock())
            native_lock["packages"][f"node_modules/{_PACKAGE_NAME}"][
                "hasInstallScript"
            ] = True
            (source / "source/package-lock.json").write_text(
                json.dumps(native_lock, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                StandardNpmLifecycleError, "install/native build lifecycle"
            ):
                load_npm_source_authority(source, target)

    def test_source_authority_preserves_each_transitive_lock_edge_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ports, _plan, source, _candidate, target = self._fixture(root)
            package_root = source / "source"
            manifest = {
                "name": "fixture-app",
                "version": "1.0.0",
                "type": "commonjs",
                "dependencies": {"is-odd": "3.0.1"},
            }
            integrity = "sha256-" + base64.b64encode(b"a" * 32).decode()
            lock = {
                "name": "fixture-app",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {
                        "name": "fixture-app",
                        "version": "1.0.0",
                        "dependencies": {"is-odd": "3.0.1"},
                    },
                    "node_modules/is-number": {
                        "version": "6.0.0",
                        "resolved": (
                            "https://registry.npmjs.org/is-number/-/is-number-6.0.0.tgz"
                        ),
                        "integrity": integrity,
                    },
                    "node_modules/is-odd": {
                        "version": "3.0.1",
                        "resolved": (
                            "https://registry.npmjs.org/is-odd/-/is-odd-3.0.1.tgz"
                        ),
                        "integrity": integrity,
                        "dependencies": {"is-number": "^6.0.0"},
                    },
                },
            }
            (package_root / "package.json").write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            (package_root / "package-lock.json").write_text(
                json.dumps(lock, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            authority = load_npm_source_authority(source, target)

            self.assertEqual(
                authority.packages,
                (
                    StandardNpmLockedPackage("is-number", "6.0.0", ()),
                    StandardNpmLockedPackage("is-odd", "3.0.1", ("is-number",)),
                ),
            )

    def test_source_authority_accepts_locked_optional_runtime_and_absent_optional_peer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ports, _plan, source, _candidate, target = self._fixture(root)
            package_root = source / "source"
            integrity = "sha256-" + base64.b64encode(b"a" * 32).decode()
            manifest = {
                "name": "fixture-app",
                "version": "1.0.0",
                "type": "commonjs",
                "dependencies": {"database-client": "8.23.0"},
            }
            lock = {
                "name": "fixture-app",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "fixture-app",
                        "version": "1.0.0",
                        "dependencies": {"database-client": "8.23.0"},
                    },
                    "node_modules/database-client": {
                        "version": "8.23.0",
                        "resolved": (
                            "https://registry.npmjs.org/database-client/"
                            "-/database-client-8.23.0.tgz"
                        ),
                        "integrity": integrity,
                        "dependencies": {"required-runtime": "1.0.0"},
                        "optionalDependencies": {"optional-runtime": "1.0.0"},
                        "peerDependencies": {"missing-native-peer": ">=3.0.1"},
                        "peerDependenciesMeta": {
                            "missing-native-peer": {"optional": True}
                        },
                    },
                    "node_modules/optional-runtime": {
                        "version": "1.0.0",
                        "resolved": (
                            "https://registry.npmjs.org/optional-runtime/"
                            "-/optional-runtime-1.0.0.tgz"
                        ),
                        "integrity": integrity,
                        "optional": True,
                    },
                    "node_modules/required-runtime": {
                        "version": "1.0.0",
                        "resolved": (
                            "https://registry.npmjs.org/required-runtime/"
                            "-/required-runtime-1.0.0.tgz"
                        ),
                        "integrity": integrity,
                        "peerDependencies": {"database-client": ">=8.0.0"},
                    },
                },
            }
            (package_root / "package.json").write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            (package_root / "package-lock.json").write_text(
                json.dumps(lock, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            authority = load_npm_source_authority(source, target)

            self.assertEqual(
                authority.packages,
                (
                    StandardNpmLockedPackage(
                        "database-client",
                        "8.23.0",
                        ("optional-runtime", "required-runtime"),
                    ),
                    StandardNpmLockedPackage("optional-runtime", "1.0.0", ()),
                    StandardNpmLockedPackage(
                        "required-runtime", "1.0.0", ("database-client",)
                    ),
                ),
            )
            inventory = {
                "name": "fixture-app",
                "version": "1.0.0",
                "dependencies": {
                    "database-client": {
                        "version": "8.23.0",
                        "dependencies": {
                            "missing-native-peer": {},
                            "optional-runtime": {"version": "1.0.0"},
                            "required-runtime": {
                                "version": "1.0.0",
                                "dependencies": {
                                    "database-client": {"version": "8.23.0"}
                                },
                            },
                        },
                    }
                },
            }
            validate_npm_inventory(json.dumps(inventory).encode(), authority)

            lock["packages"]["node_modules/database-client"][
                "peerDependenciesMeta"
            ] = {}
            (package_root / "package-lock.json").write_text(
                json.dumps(lock, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                StandardNpmLifecycleError, "required peer dependency"
            ):
                load_npm_source_authority(source, target)

    def test_reparse_point_in_installed_tree_is_rejected_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, source, candidate, _target = self._fixture(root)
            calls: list[dict[str, object]] = []
            successful = self._successful_npm(calls)

            def install_with_reparse(command, **kwargs):
                result = successful(command, **kwargs)
                if "ci" in command:
                    installed = kwargs["cwd"] / "node_modules" / _PACKAGE_NAME
                    (installed / "reparse").write_text("opaque", encoding="utf-8")
                return result

            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=install_with_reparse,
                ),
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.path_is_link_or_reparse",
                    side_effect=lambda path: (
                        path.name == "reparse" or path_is_link_or_reparse(path)
                    ),
                ),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError, "link, reparse point, or special file"
                ),
            ):
                ports.build(plan, ())

            self.assertEqual(list(ports.object_root.iterdir()), [])
            self.assertEqual(
                local_generated_source_tree_identity(source), candidate.tree_identity
            )

    def test_native_addon_payload_is_rejected_before_artifact_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, source, candidate, _target = self._fixture(root)
            calls: list[dict[str, object]] = []
            successful = self._successful_npm(calls)

            def install_with_native_addon(command, **kwargs):
                result = successful(command, **kwargs)
                if "ci" in command:
                    installed = kwargs["cwd"] / "node_modules" / _PACKAGE_NAME
                    (installed / "payload.node").write_bytes(b"\x7fELFfixture")
                return result

            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=install_with_native_addon,
                ),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError,
                    "native or WebAssembly payload",
                ),
            ):
                ports.build(plan, ())

            self.assertEqual(list(ports.object_root.iterdir()), [])
            self.assertEqual(
                local_generated_source_tree_identity(source), candidate.tree_identity
            )

    def test_installed_tree_rejects_native_and_wasm_magic_families(self) -> None:
        for label, magic in (
            ("elf", b"\x7fELF"),
            ("pe", b"MZ"),
            ("macho", b"\xcf\xfa\xed\xfe"),
            ("fat-macho", b"\xca\xfe\xba\xbe"),
            ("static-archive", b"!<arch>\n"),
            ("wasm", b"\x00asm"),
        ):
            with (
                self.subTest(payload=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                installed = Path(temporary) / "node_modules/package"
                installed.mkdir(parents=True)
                (installed / "payload.bin").write_bytes(magic + b"fixture")
                with self.assertRaisesRegex(
                    LocalStandardLifecycleError,
                    "native or WebAssembly payload",
                ):
                    LocalStandardLifecyclePorts._require_regular_npm_tree(
                        installed.parent
                    )

    def test_installed_tree_prunes_reparse_directories_and_bounds_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "node_modules"
            junction = installed / "junction"
            junction.mkdir(parents=True)
            (junction / "must-not-be-visited.js").write_text(
                "module.exports = true;\n", encoding="utf-8"
            )
            with (
                mock.patch.object(
                    Path,
                    "rglob",
                    side_effect=AssertionError(
                        "npm tree validation must enumerate incrementally"
                    ),
                ),
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.path_is_link_or_reparse",
                    side_effect=lambda path: (
                        path == junction or path_is_link_or_reparse(path)
                    ),
                ),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError,
                    "link, reparse point, or special file",
                ),
            ):
                LocalStandardLifecyclePorts._require_regular_npm_tree(installed)

        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "node_modules"
            installed.mkdir()
            (installed / "one.js").write_text("one\n", encoding="utf-8")
            (installed / "two.js").write_text("two\n", encoding="utf-8")
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local._MAX_NPM_INSTALLED_ENTRIES",
                    2,
                ),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError,
                    "entry limit",
                ),
            ):
                LocalStandardLifecyclePorts._require_regular_npm_tree(installed)

    def test_inventory_collapses_identical_hoisted_npm_references(self) -> None:
        authority = StandardNpmSourceAuthority(
            "source/package.json",
            "source/package-lock.json",
            _identity("two-dependency-manifest"),
            _identity("two-dependency-lock"),
            "fixture-app",
            "1.0.0",
            ("is-number", "is-odd"),
            (
                StandardNpmLockedPackage("is-number", "6.0.0", ()),
                StandardNpmLockedPackage("is-odd", "3.0.1", ("is-number",)),
            ),
        )
        inventory = {
            "name": "fixture-app",
            "version": "1.0.0",
            "dependencies": {
                "is-number": {"version": "6.0.0", "dependencies": {}},
                "is-odd": {
                    "version": "3.0.1",
                    "dependencies": {
                        "is-number": {
                            "version": "6.0.0",
                            "dependencies": {},
                        }
                    },
                },
            },
        }

        retained = validate_npm_inventory(json.dumps(inventory).encode(), authority)
        self.assertIn(b'"name":"is-number"', retained)

        inventory["dependencies"]["is-odd"]["dependencies"]["is-number"]["version"] = (
            "7.0.0"
        )
        with self.assertRaisesRegex(
            StandardNpmLifecycleError, "conflicting views of package 'is-number'"
        ):
            validate_npm_inventory(json.dumps(inventory).encode(), authority)

        repeated = inventory["dependencies"]["is-odd"]["dependencies"]["is-number"]
        repeated["version"] = "6.0.0"
        repeated["extraneous"] = True
        with self.assertRaisesRegex(
            StandardNpmLifecycleError, "invalid package 'is-number'"
        ):
            validate_npm_inventory(json.dumps(inventory).encode(), authority)

    def test_same_runtime_cache_rejects_self_consistent_artifact_tampering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _source, _candidate, _target = self._fixture(root)
            calls: list[dict[str, object]] = []
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=self._successful_npm(calls),
                ),
                mock.patch.object(
                    ports,
                    "_run",
                    return_value=subprocess.CompletedProcess((), 0, "", ""),
                ),
            ):
                output = ports.build(plan, ())

            artifact_root = ports.artifact_path(output.exports[0]).parent
            installed = artifact_root / "app/source/node_modules"
            (installed / _PACKAGE_NAME / "index.js").write_text(
                "module.exports = () => 'tampered';\n", encoding="utf-8"
            )
            evidence_path = artifact_root / ".literate/npm/evidence-manifest.json"
            evidence = json.loads(evidence_path.read_bytes())
            evidence["installed_tree_identity"] = local_tree_identity(installed).uri
            evidence_path.write_bytes(canonical_json_bytes(evidence))
            artifact_manifest_path = artifact_root / "artifact-manifest.json"
            artifact_manifest = json.loads(artifact_manifest_path.read_bytes())
            artifact_manifest["tree"] = _local_tree_identity(
                artifact_root,
                excluded=frozenset({"artifact-manifest.json"}),
            ).uri
            artifact_manifest_path.write_bytes(canonical_json_bytes(artifact_manifest))

            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process"
                ) as npm,
                self.assertRaisesRegex(
                    LocalStandardLifecycleError, "changed after publication"
                ),
            ):
                ports.build(plan, ())
            npm.assert_not_called()

    def test_inventory_unions_sparse_nonleaf_dedup_views(self) -> None:
        authority = StandardNpmSourceAuthority(
            "source/package.json",
            "source/package-lock.json",
            _identity("dedup-manifest"),
            _identity("dedup-lock"),
            "fixture-app",
            "1.0.0",
            ("braces", "micromatch"),
            (
                StandardNpmLockedPackage("braces", "3.0.3", ("fill-range",)),
                StandardNpmLockedPackage("fill-range", "7.1.1", ()),
                StandardNpmLockedPackage("micromatch", "4.0.8", ("braces",)),
            ),
        )
        inventory = {
            "name": "fixture-app",
            "version": "1.0.0",
            "dependencies": {
                "braces": {
                    "version": "3.0.3",
                    "dependencies": {
                        "fill-range": {
                            "version": "7.1.1",
                            "dependencies": {},
                        }
                    },
                },
                "micromatch": {
                    "version": "4.0.8",
                    "dependencies": {
                        "braces": {"version": "3.0.3"},
                    },
                },
            },
        }

        retained = validate_npm_inventory(json.dumps(inventory).encode(), authority)
        self.assertIn(b'"name":"fill-range"', retained)

        inventory["dependencies"]["micromatch"]["dependencies"]["braces"]["version"] = (
            "3.0.2"
        )
        with self.assertRaisesRegex(
            StandardNpmLifecycleError, "conflicting views of package 'braces'"
        ):
            validate_npm_inventory(json.dumps(inventory).encode(), authority)

    def test_new_runtime_hits_durable_external_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, plan, _source, _candidate, _target = self._fixture(root)
            calls: list[dict[str, object]] = []
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=self._successful_npm(calls),
                ),
                mock.patch.object(
                    first,
                    "_run",
                    return_value=subprocess.CompletedProcess((), 0, "", ""),
                ),
            ):
                expected = first.build(plan, ())

            second = LocalStandardLifecyclePorts(
                source_trees=first.source_trees,
                object_root=first.object_root,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
                npm_targets=tuple(first.npm_targets.values()),
                provider_environment=first.provider_environment,
                dependency_observation=first.dependency_observation,
            )
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=self._successful_npm(calls),
                ),
                mock.patch.object(
                    second,
                    "_run",
                    return_value=subprocess.CompletedProcess((), 0, "", ""),
                ),
            ):
                replayed = second.build(plan, ())
                cached = second.build(plan, ())

            self.assertEqual(replayed, expected)
            self.assertEqual(cached, expected)
            self.assertEqual(len(calls), 2)
            self.assertEqual(second.build_cache_misses, 0)
            self.assertEqual(second.build_cache_hits, 2)

    def test_copied_npm_cache_without_checkpoint_is_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, plan, _source, _candidate, _target = self._fixture(root)
            calls: list[dict[str, object]] = []
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=self._successful_npm(calls),
                ),
                mock.patch.object(
                    first,
                    "_run",
                    return_value=subprocess.CompletedProcess((), 0, "", ""),
                ),
            ):
                expected = first.build(plan, ())

            isolated = root / "isolated-objects"
            copy_digest_cache_without_sidecars(first.object_root, isolated)
            second = LocalStandardLifecyclePorts(
                source_trees=first.source_trees,
                object_root=isolated,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
                npm_targets=tuple(first.npm_targets.values()),
                provider_environment=first.provider_environment,
                dependency_observation=first.dependency_observation,
            )
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.run_bounded_process",
                    side_effect=self._successful_npm(calls),
                ),
                mock.patch.object(
                    second,
                    "_run",
                    return_value=subprocess.CompletedProcess((), 0, "", ""),
                ),
            ):
                replayed = second.build(plan, ())
                cached = second.build(plan, ())

            self.assertEqual(replayed, expected)
            self.assertEqual(cached, expected)
            self.assertEqual(len(calls), 4)
            self.assertEqual(second.build_cache_misses, 1)
            self.assertEqual(second.build_cache_hits, 1)


if __name__ == "__main__":
    unittest.main()
