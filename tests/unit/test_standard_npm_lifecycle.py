"""Selection-bound npm replay through the local Standard lifecycle."""

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

from literate_ai.adapters.builders import BoundedProcessResult
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    StandardNpmTarget,
    local_generated_source_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_npm import (
    load_npm_source_authority,
    parse_npm_source_authority,
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
)
from tests.support.fixtures_test_component_node_generation_preparation import (
    _fixture as _generation_fixture,
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


if __name__ == "__main__":
    unittest.main()
