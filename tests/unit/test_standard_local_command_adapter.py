"""Provider-neutral command authority realized by the local Standard adapter."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.builders import discover_python_toolchain
from literate_ai.adapters.component_acceptance import (
    SERVICE_SCHEMA,
    ComponentAcceptanceError,
    ComponentAcceptanceOracleBinding,
    ComponentAcceptanceOracleBundle,
    load_persistent_service_acceptance,
)
from literate_ai.adapters.generation_preparation import (
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalIndependentAcceptanceCase,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    local_generated_source_tree_identity,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import (
    GeneratedCandidateCommandError,
    _child_process_environment,
    _directory_export_bytes,
    _host_process_environment,
    _local_tree_identity,
    _require_generated_export,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_NODE_RUNTIME_DRIVER,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    verify_qualification_boms,
    verify_qualification_build,
    verify_qualification_build_authorization,
    verify_qualification_command_authority,
    verify_qualification_execution,
    verify_qualification_generated_suite,
    verify_qualification_generated_tests,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    realize_manifest,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_inputs,
)
from literate_ai.contracts import (
    ArtifactAssemblyDependency,
    ArtifactExport,
    BlobRef,
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentEntrypointCommandContract,
    ComponentLifecycleCommand,
    ContentIdentity,
    ContractValidationError,
    LibraryCapabilityImport,
    LibraryImportSurface,
    StandardBuildEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestExecutionEvidence,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.security import AuthorizationError
from tests.unit.standard_source_evidence_fixture import register_strict_source
from tests.unit.test_component_execution_planning import _diamond_lock, _models
from tests.unit.test_component_node_generation_preparation import _fixture


def _identity(label: str):
    return canonical_identity({"standard-local-command-test": label})


def rewrite_self_authenticating_artifact(
    artifact_root: Path, export_id: str, payload: bytes
) -> None:
    """Rewrite export bytes and the self-declared tree field together."""

    export = artifact_root / export_id
    export.write_bytes(payload)
    manifest_path = artifact_root / "artifact-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["tree"] = _local_tree_identity(
        artifact_root, excluded=frozenset({"artifact-manifest.json"})
    ).uri
    manifest_path.write_bytes(canonical_json_bytes(manifest))


def copy_digest_cache_without_sidecars(source: Path, destination: Path) -> None:
    """Copy digest-named artifact directories and omit sibling checkpoint files."""

    destination.mkdir(parents=True)
    for child in source.iterdir():
        if not child.is_dir() or len(child.name) != 64:
            continue
        shutil.copytree(child, destination / child.name)


def _python_copy_lifecycle(root: Path):
    snapshot, execution = _fixture()
    generation_plan = execution.generation_plans[0]
    binding = LocalComponentToolBinding(sys.executable)
    build_script = (
        "from pathlib import Path; import shutil,sys; "
        "shutil.copy2(Path(sys.argv[1])/'app.py', Path(sys.argv[2]))"
    )
    commands = (
        ComponentLifecycleCommand(
            ComponentCommandPhase.BUILD,
            (
                "{tool}",
                "-c",
                build_script,
                "{source_root}",
                "{export_path}",
                "{object_root}",
                "{provider_artifacts}",
            ),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.TEST,
            (
                "{tool}",
                "-c",
                "from pathlib import Path; import json,sys; "
                "print(json.dumps(dict(schema="
                "'literate-ai/generated-test-results@1',cases=["
                "dict(case_id='fixture-example',outcome='passed')]),"
                "sort_keys=True,separators=(',',':')))",
                "{artifact_root}",
            ),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.EXECUTE,
            (
                "{tool}",
                "-c",
                "from pathlib import Path; import sys; "
                "print((Path(sys.argv[1])/'app').read_text().strip())",
                "{artifact_root}",
                "--litai-smoke",
            ),
        ),
    )
    contract = ComponentCommandContract(
        component_revision=generation_plan.component_revision,
        locked_build_authority_identity=_identity("locked-build-authority"),
        build_system_resolver_identity=_identity("build-system-resolver"),
        build_system_toolchain_identity=_identity("build-system-toolchain"),
        language_compiler_identity=binding.toolchain_identity,
        language_runtime_identity=_identity("language-runtime"),
        commands=commands,
        tool_bindings=tuple(
            ComponentCommandToolBinding(phase, binding.toolchain_identity)
            for phase in ComponentCommandPhase
        ),
        artifact_export=ComponentArtifactExportShape(
            "app",
            "executable",
            _identity("abi"),
            _identity("target"),
            "application/vnd.literate-ai.executable",
            _identity("producer"),
        ),
    )
    source = root / "source"
    source.mkdir()
    (source / "app.py").write_bytes(b"known-output\n")
    registry = LocalSourceTreeRegistry()
    candidate = register_strict_source(
        registry,
        source,
        snapshot=snapshot,
        generation_plan=generation_plan,
        identity_namespace="standard-local-command-test",
    )
    ports = LocalStandardLifecyclePorts(
        source_trees=registry,
        object_root=root / "objects",
        contracts=(contract,),
        tool_bindings=(binding,),
    )
    intent = ports.create(execution, generation_plan, candidate, (), ())
    index = ports.index(candidate.component_revision, candidate.tree_identity)
    plan = ports.finalize(intent, ports.authorize(intent, index))
    return ports, plan, candidate, intent


def _provider_export(export_id: str) -> ArtifactExport:
    payload = export_id.encode()
    return ArtifactExport(
        export_id=export_id,
        component_revision=_identity(f"{export_id}-revision"),
        role="static-library",
        abi_identity=_identity("abi"),
        target_identity=_identity("target"),
        media_type="application/x-native",
        producer_identity=_identity("producer"),
        source_tree_identity=_identity("source-tree"),
        toolchain_identity=_identity("toolchain"),
        authorization_identity=_identity("authorization"),
        dependency_artifact_identities=(),
        blob=BlobRef(
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            media_type=("application/x-native"),
        ),
    )


class StandardLocalCommandAdapterTests(unittest.TestCase):
    def assert_reopened_build(self, recorder, plan, build):
        def reader():
            return QualificationEvidenceReader(
                recorder.entries, max_bytes=5_000_000, max_records=1000
            )

        verify_qualification_build(reader(), plan=plan, build=build)
        observation = reader().read_json(build.build_observation_identity)
        before = reader().read_json(
            ContentIdentity.parse_uri(observation["artifact_tree_identity"])
        )
        custody = reader().read_json(build.artifact_custody_identity)
        after = reader().read_json(
            ContentIdentity.parse_uri(custody["artifact_tree_identity"])
        )
        self.assertEqual(before["schema"], "literate-ai/local-source-tree@1")
        self.assertEqual(after["schema"], before["schema"])
        manifest_entry = next(
            item for item in after["files"] if item["path"] == "artifact-manifest.json"
        )
        self.assertEqual(
            before["files"],
            [item for item in after["files"] if item != manifest_entry],
        )
        manifest = json.loads(
            reader().read_bytes(
                ContentIdentity.parse_uri("sha256:" + manifest_entry["sha256"])
            )
        )
        self.assertEqual(manifest["tree"], observation["artifact_tree_identity"])
        self.assertEqual(
            manifest["build_observation"], build.build_observation_identity.uri
        )
        self.assertNotEqual(
            custody["artifact_tree_identity"], observation["artifact_tree_identity"]
        )
        for missing in (
            build.artifact_custody_identity,
            ContentIdentity.parse_uri(observation["artifact_tree_identity"]),
            ContentIdentity.parse_uri(custody["artifact_tree_identity"]),
            ContentIdentity.parse_uri("sha256:" + manifest_entry["sha256"]),
        ):
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                verify_qualification_build(
                    QualificationEvidenceReader(
                        tuple(item for item in recorder.entries if item[0] != missing),
                        max_bytes=5_000_000,
                        max_records=1000,
                    ),
                    plan=plan,
                    build=build,
                )
        for files in (
            before["files"],
            after["files"] + [after["files"][0]],
            after["files"]
            + [{"path": "../foreign", "sha256": manifest_entry["sha256"]}],
            after["files"] + [{"path": "extra", "sha256": manifest_entry["sha256"]}],
            [
                {**item, "sha256": "0" * 64} if item != manifest_entry else item
                for item in after["files"]
            ],
        ):
            altered_tree = recorder.remember_json({**after, "files": files})
            altered_custody = recorder.remember_json(
                {**custody, "artifact_tree_identity": altered_tree.uri}
            )
            with (
                self.subTest(files=files),
                self.assertRaisesRegex(QualificationCaptureError, "artifact-tree-"),
            ):
                verify_qualification_build(
                    reader(),
                    plan=plan,
                    build=replace(build, artifact_custody_identity=altered_custody),
                )
        raw_manifest = reader().read_bytes(
            ContentIdentity.parse_uri("sha256:" + manifest_entry["sha256"])
        )
        for payload in (
            json.dumps(
                {**manifest, "tree": build.identity.uri},
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            json.dumps(
                {**manifest, "build_observation": build.identity.uri},
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            json.dumps(
                {**manifest, "provider_materials": None},
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            b'{"tree":"duplicate",' + raw_manifest[1:],
        ):
            changed_manifest = recorder.remember_bytes(payload)
            changed_tree = recorder.remember_json(
                {
                    **after,
                    "files": [
                        {**item, "sha256": changed_manifest.digest}
                        if item == manifest_entry
                        else item
                        for item in after["files"]
                    ],
                }
            )
            changed_custody = recorder.remember_json(
                {**custody, "artifact_tree_identity": changed_tree.uri}
            )
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(
                    QualificationCaptureError, "artifact-tree-mismatch"
                ),
            ):
                verify_qualification_build(
                    reader(),
                    plan=plan,
                    build=replace(build, artifact_custody_identity=changed_custody),
                )
        process = reader().read_json(
            ContentIdentity.parse_uri(observation["process_observation_identity"])
        )
        for changes in (
            {"phase": "execute"},
            {"returncode": False},
            {"returncode": 1},
            {"accepted": True},
        ):
            process_id = recorder.remember_json({**process, **changes})
            observation_id = recorder.remember_json(
                {**observation, "process_observation_identity": process_id.uri}
            )
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(
                    QualificationCaptureError, "build-process-mismatch"
                ),
            ):
                verify_qualification_build(
                    reader(),
                    plan=plan,
                    build=replace(build, build_observation_identity=observation_id),
                )

    def assert_reopened_generated_tests(
        self, recorder, plan, build, tests, recipe, component_lock
    ):
        def reader(entries=None):
            return QualificationEvidenceReader(
                recorder.entries if entries is None else entries,
                max_bytes=5_000_000,
                max_records=1000,
            )

        verify_qualification_boms(reader(), component_lock=component_lock, build=build)
        for missing in (
            build.source_sbom.bom_identity,
            build.resolved_sbom.bom_identity,
            build.source_sbom.managed_graph_identity,
        ):
            with (
                self.subTest(missing_bom=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                verify_qualification_boms(
                    reader(
                        tuple(item for item in recorder.entries if item[0] != missing)
                    ),
                    component_lock=component_lock,
                    build=build,
                )
        for field in ("source_sbom", "resolved_sbom"):
            binding = getattr(build, field)
            with (
                self.subTest(binding=field),
                self.assertRaisesRegex(
                    QualificationCaptureError, "bom-binding-mismatch"
                ),
            ):
                verify_qualification_boms(
                    reader(),
                    component_lock=component_lock,
                    build=replace(
                        build,
                        **{
                            field: replace(
                                binding, component_count=binding.component_count + 1
                            )
                        },
                    ),
                )

        for field in ("source_sbom", "resolved_sbom"):
            binding = getattr(build, field)
            document = json.loads(reader().read_bytes(binding.bom_identity))
            document["metadata"]["component"]["name"] = "foreign-component"
            altered = recorder.remember_json(document)
            replacements = {field: replace(binding, bom_identity=altered)}
            if field == "source_sbom":
                replacements["resolved_sbom"] = replace(
                    build.resolved_sbom, source_bom_identity=altered
                )
            with (
                self.subTest(foreign_bom=field),
                self.assertRaisesRegex(QualificationCaptureError, "bom-invalid"),
            ):
                verify_qualification_boms(
                    reader(),
                    component_lock=component_lock,
                    build=replace(build, **replacements),
                )

        def verify(evidence, retained=None):
            verify_qualification_generated_tests(
                reader(retained),
                build_plan_identity=plan.identity,
                build=build,
                tests=evidence,
            )

        def verify_suite(evidence, current_recipe=recipe, retained=None):
            verify_qualification_generated_suite(
                reader(retained),
                recipe=current_recipe,
                component_lock_identity=recipe.component_lock_identity,
                tests=evidence,
            )

        verify_suite(tests)
        with self.assertRaisesRegex(QualificationCaptureError, "suite-invalid"):
            verify_suite(
                tests, replace(recipe, recipe_id=recipe.recipe_id + "-changed")
            )
        with self.assertRaisesRegex(
            QualificationCaptureError, "suite-authority-invalid"
        ):
            verify_qualification_generated_suite(
                reader(),
                recipe=recipe,
                tests=tests,
                component_lock_identity=_identity("foreign-lock"),
            )
        with self.assertRaisesRegex(QualificationCaptureError, "record-missing"):
            verify_suite(
                tests,
                retained=tuple(
                    item
                    for item in recorder.entries
                    if item[0] != tests.generated_test_suite_identity
                ),
            )
        suite = json.loads(reader().read_bytes(tests.generated_test_suite_identity))
        suite["cases"][0]["specification_refs"] = ["foreign.md"]
        changed_suite = recorder.remember_bytes(json.dumps(suite).encode())
        with self.assertRaisesRegex(QualificationCaptureError, "suite-invalid"):
            verify_suite(replace(tests, generated_test_suite_identity=changed_suite))
        if tests.entrypoint_evidence is None:
            remaining = tests.cases[:-1]
            with self.assertRaisesRegex(
                QualificationCaptureError, "suite-membership-mismatch"
            ):
                verify_suite(
                    replace(
                        tests,
                        cases=remaining,
                        selected_case_identities=tuple(
                            case.case_identity for case in remaining
                        ),
                        selected_count=len(remaining),
                        executed_count=len(remaining),
                        passed_count=len(remaining),
                    )
                )

        verify(tests)
        cases = (
            tests.cases
            if tests.entrypoint_evidence is None
            else tests.entrypoint_evidence[0].cases
        )
        first = reader().read_json(cases[0].observation_identity)
        process_id = ContentIdentity.parse_uri(
            first["suite_process_observation_identity"]
        )
        process = reader().read_json(process_id)
        required = (
            cases[0].case_identity,
            cases[0].observation_identity,
            process_id,
            ContentIdentity.parse_uri(process["stdout_identity"]),
            ContentIdentity.parse_uri(process["stderr_identity"]),
        )
        for missing in required:
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                verify(
                    tests,
                    tuple(item for item in recorder.entries if item[0] != missing),
                )
        if tests.entrypoint_evidence is not None:
            unit = tests.entrypoint_evidence[0]
            changed_case = replace(
                unit.cases[0],
                observation_identity=recorder.remember_json(
                    {
                        **first,
                        "entrypoint_identity": _identity("foreign-entrypoint").uri,
                    }
                ),
            )
            changed_unit = replace(unit, cases=(changed_case, *unit.cases[1:]))
            recorder.remember_json(changed_unit.to_dict())
            units = (changed_unit, *tests.entrypoint_evidence[1:])
            aggregate_cases = tuple(
                replace(case, observation_identity=changed_case.observation_identity)
                if case.observation_identity == unit.cases[0].observation_identity
                else case
                for case in tests.cases
            )
            runner = recorder.remember_json(
                {
                    "schema": "literate-ai/multi-entrypoint-test-runners@1",
                    "entrypoint_evidence": [item.identity.uri for item in units],
                }
            )
            custody = recorder.remember_json(
                {
                    "schema": "literate-ai/multi-entrypoint-test-custody@1",
                    "entrypoint_evidence": [item.identity.uri for item in units],
                }
            )
            with self.assertRaisesRegex(
                QualificationCaptureError, "test-process-mismatch"
            ):
                verify(
                    replace(
                        tests,
                        cases=aggregate_cases,
                        entrypoint_evidence=units,
                        runner_identity=runner,
                        test_custody_identity=custody,
                    )
                )
            return
        stdout = reader().read_json(
            ContentIdentity.parse_uri(process["stdout_identity"])
        )
        result = json.loads(stdout)
        bad_outputs = (
            {**result, "cases": result["cases"][:-1]},
            {**result, "cases": result["cases"] + result["cases"][:1]},
            {
                **result,
                "cases": [{**case, "outcome": "failed"} for case in result["cases"]],
            },
            {**result, "accepted": True},
        )
        changes = [
            {"phase": "execute"},
            {"plan_identity": _identity("foreign-plan").uri},
            {"returncode": False},
            {"returncode": 1},
            {"accepted": True},
        ]
        changes += [
            {"stdout_identity": recorder.remember_json(json.dumps(output)).uri}
            for output in bad_outputs
        ]
        changes += [
            {"stdout_identity": recorder.remember_json(None).uri},
            {
                "stdout_identity": recorder.remember_json(
                    "[" * 2000 + "0" + "]" * 2000
                ).uri
            },
            {"stderr_identity": recorder.remember_json(False).uri},
            {
                "stdout_identity": recorder.remember_json(
                    '{"schema":"bad","schema":"literate-ai/generated-test-results@1","cases":'
                    + json.dumps(result["cases"])
                    + "}"
                ).uri
            },
        ]
        for change in changes:
            altered_process = recorder.remember_json({**process, **change})
            altered_cases = tuple(
                replace(
                    case,
                    observation_identity=recorder.remember_json(
                        {
                            **reader().read_json(case.observation_identity),
                            "suite_process_observation_identity": altered_process.uri,
                        }
                    ),
                )
                for case in cases
            )
            with (
                self.subTest(change=change),
                self.assertRaises(QualificationCaptureError),
            ):
                verify(replace(tests, cases=altered_cases))

    def assert_late_provider_execution(
        self, ports, execution_plan, plan, exports, recorder
    ):
        original_scope = plan_standard_execution_inputs(
            execution_plan, plan, exports, ()
        )
        compiled_identity = plan.identity
        output_identities = tuple(item.identity for item in exports)
        payload = b"runtime-output\n"
        # Supply an admitted provider, as the controller will do after acceptance.
        for directory in (False, True):
            with (
                self.subTest(directory=directory),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                export_path = root / "runtime-provider"
                if directory:
                    export_path.mkdir()
                    artifact = export_path / "entry"
                else:
                    artifact = export_path
                artifact.write_bytes(payload)
                content = _directory_export_bytes(export_path) if directory else payload
                provider = replace(
                    exports[0],
                    export_id="runtime-provider",
                    component_revision=_identity("late-runtime-provider"),
                    blob=replace(
                        exports[0].blob,
                        digest=hashlib.sha256(content).hexdigest(),
                        size=len(content),
                    ),
                )
                dependency = ArtifactAssemblyDependency(
                    exports[0].identity,
                    provider.identity,
                    DependencyKind.RUNTIME,
                    _identity("admitted-runtime-edge"),
                    _identity("provider-acceptance"),
                )
                scope = replace(original_scope, runtime_dependencies=(dependency,))
                with (
                    mock.patch.dict(
                        ports._exports_by_identity, {provider.identity.uri: provider}
                    ),
                    mock.patch.dict(
                        ports._artifact_paths, {provider.identity.uri: root}
                    ),
                    mock.patch.dict(
                        ports._artifact_blob_paths, {provider.blob.identity: artifact}
                    ),
                    mock.patch.dict(
                        ports._artifact_blob_bytes,
                        {provider.blob.identity: content} if directory else {},
                    ),
                    mock.patch.dict(
                        ports.provider_environment,
                        {
                            provider.export_id: (
                                "LITAI_TEST_RUNTIME",
                                artifact.relative_to(root).as_posix(),
                            )
                        },
                    ),
                ):
                    execution = ports.execute_scoped(plan, exports, scope, (provider,))
                    self.assertEqual(
                        ports.execution_stdout[plan.component_revision.uri],
                        "runtime-output",
                    )
                    self.assertEqual(
                        execution.provider_artifact_identities, (provider.identity,)
                    )
                    self.assert_reopened_execution(recorder, plan, execution)
                    artifact.write_bytes(b"corrupted-provider\n")
                    if directory:
                        # A still-valid cached ZIP must not hide changed live inputs.
                        self.assertEqual(
                            ports.read_artifact_blob(provider.blob), content
                        )
                    with mock.patch.object(ports, "_run_locked") as launch:
                        with self.assertRaisesRegex(
                            LocalStandardLifecycleError, "artifact blob changed"
                        ):
                            ports.execute_scoped(plan, exports, scope, (provider,))
                        launch.assert_not_called()
        self.assertEqual(plan.identity, compiled_identity)
        self.assertEqual(tuple(item.identity for item in exports), output_identities)
        self.assertEqual(plan.provider_artifact_identities, ())

    def assert_scoped_execution(self, ports, execution_plan, plan, exports, recorder):
        scope = plan_standard_execution_inputs(execution_plan, plan, exports, ())
        executed = ports.execute_scoped(plan, exports, scope, ())
        self.assertEqual(executed.execution_authority.input_scope, scope)
        self.assertEqual(type(executed).from_dict(executed.to_dict()), executed)
        self.assert_reopened_execution(recorder, plan, executed)
        # Reopen actual single/multi-entrypoint stdout without another command.
        expected_stdout = ports.execution_stdout.pop(plan.component_revision.uri)
        del ports._execution_evidence[executed.identity.uri]
        with mock.patch.object(
            ports, "_run_locked", side_effect=AssertionError("controller command")
        ):
            self.assertEqual(
                ports.admit_transferred_execution(
                    plan=plan,
                    exports=exports,
                    evidence=executed,
                    records=recorder.entries,
                    admission_guard=lambda: None,
                    scope=scope,
                    provider_artifacts=(),
                ),
                executed,
            )
        self.assertEqual(
            ports.execution_stdout[plan.component_revision.uri], expected_stdout
        )
        authority = executed.execution_authority
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        units = executed.entrypoint_evidence or (executed,)
        for unit in units:
            process = reader.read_json(unit.observation_identity)
            self.assertEqual(
                process["execution_authority_identity"], authority.identity.uri
            )
        with mock.patch.object(ports, "_run_locked") as launch:
            with self.assertRaisesRegex(LocalStandardLifecycleError, "execution scope"):
                ports.execute_scoped(
                    plan,
                    exports,
                    replace(scope, build_plan_identity=_identity("other-build")),
                    (),
                )
            with self.assertRaisesRegex(LocalStandardLifecycleError, "execution scope"):
                ports.execute_scoped(plan, exports, scope, exports)
            expired = replace(
                authority,
                grant=replace(
                    authority.grant,
                    issued_at=datetime(2020, 1, 1, tzinfo=UTC),
                    expires_at=datetime(2020, 1, 2, tzinfo=UTC),
                ),
            )
            with mock.patch.object(
                ports, "authorize_execution_inputs", return_value=expired
            ):
                with self.assertRaisesRegex(AuthorizationError, "expired"):
                    ports.execute_scoped(plan, exports, scope, ())
            launch.assert_not_called()

        # Expiry during a command prevents successful evidence and the next unit.
        actual_launch = ports._run_locked
        before_evidence = set(ports._execution_evidence)
        with mock.patch.object(
            ports, "clock", return_value=authority.grant.issued_at
        ) as clock:

            def expire_after_launch(*args, **kwargs):
                result = actual_launch(*args, **kwargs)
                clock.return_value = authority.grant.expires_at + timedelta(seconds=1)
                return result

            with (
                mock.patch.object(
                    ports, "authorize_execution_inputs", return_value=authority
                ),
                mock.patch.object(
                    ports, "_run_locked", side_effect=expire_after_launch
                ) as launch,
                self.assertRaisesRegex(AuthorizationError, "expired"),
            ):
                ports.execute_scoped(plan, exports, scope, ())
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(set(ports._execution_evidence), before_evidence)

    def assert_reopened_execution(self, recorder, plan, execution):
        def reader():
            return QualificationEvidenceReader(
                recorder.entries, max_bytes=5_000_000, max_records=1000
            )

        verify_qualification_execution(
            reader(), build_plan_identity=plan.identity, execution=execution
        )
        if execution.execution_authority is None:
            with self.assertRaisesRegex(
                QualificationCaptureError, "execution-provider-mismatch"
            ):
                verify_qualification_execution(
                    reader(),
                    build_plan_identity=plan.identity,
                    execution=replace(
                        execution,
                        provider_artifact_identities=(
                            _identity("substituted-provider"),
                        ),
                    ),
                )
        else:
            with self.assertRaises(ContractValidationError):
                replace(
                    execution,
                    provider_artifact_identities=(_identity("substituted-provider"),),
                )
            with self.assertRaisesRegex(
                QualificationCaptureError, "execution-authority-mismatch"
            ):
                verify_qualification_execution(
                    reader(),
                    build_plan_identity=plan.identity,
                    execution=execution,
                    expected_execution_plan_identity=_identity(
                        "different-project-plan"
                    ),
                )
        for missing in (
            plan.identity,
            *(
                (
                    execution.execution_authority.identity,
                    execution.execution_authority.input_scope.identity,
                    execution.execution_authority.command_contract_identity,
                    execution.build_evidence_identity,
                )
                if execution.execution_authority is not None
                else ()
            ),
            execution.observation_identity,
            execution.stdout_identity,
            execution.stderr_identity,
            *(unit.identity for unit in execution.entrypoint_evidence or ()),
        ):
            incomplete = QualificationEvidenceReader(
                tuple(item for item in recorder.entries if item[0] != missing),
                max_bytes=5_000_000,
                max_records=1000,
            )
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                verify_qualification_execution(
                    incomplete, build_plan_identity=plan.identity, execution=execution
                )
        if execution.entrypoint_evidence is None:
            original = reader().read_json(execution.observation_identity)
            for changes in (
                {"returncode": False},
                {"returncode": 1},
                {"phase": "test"},
                {"plan_identity": _identity("foreign-plan").uri},
                {"stdout_identity": _identity("foreign-output").uri},
                {"accepted": True},
                {"execution_authority_identity": _identity("foreign-grant").uri},
            ):
                forged = recorder.remember_json({**original, **changes})
                with (
                    self.subTest(changes=changes),
                    self.assertRaisesRegex(
                        QualificationCaptureError, "execution-process-mismatch"
                    ),
                ):
                    verify_qualification_execution(
                        reader(),
                        build_plan_identity=plan.identity,
                        execution=replace(execution, observation_identity=forged),
                    )
            for content in ({}, [], False):
                output = recorder.remember_json(content)
                observation = recorder.remember_json(
                    {**original, "stdout_identity": output.uri}
                )
                with (
                    self.subTest(content=content),
                    self.assertRaisesRegex(
                        QualificationCaptureError, "execution-output-invalid"
                    ),
                ):
                    verify_qualification_execution(
                        reader(),
                        build_plan_identity=plan.identity,
                        execution=replace(
                            execution,
                            observation_identity=observation,
                            stdout_identity=output,
                        ),
                    )
        else:
            if execution.execution_authority is not None:
                units = execution.entrypoint_evidence
                swapped = tuple(
                    replace(unit, export_identity=units[-1 - index].export_identity)
                    for index, unit in enumerate(units)
                )
                for unit in swapped:
                    recorder.remember_json(unit.to_dict())
                identities = [unit.identity.uri for unit in swapped]
                forged = replace(
                    execution,
                    entrypoint_evidence=swapped,
                    observation_identity=recorder.remember_json(
                        {
                            "schema": (
                                "literate-ai/multi-entrypoint-execution-observation@1"
                            ),
                            "entrypoint_evidence": identities,
                        }
                    ),
                    execution_contract_identity=recorder.remember_json(
                        {
                            "schema": (
                                "literate-ai/multi-entrypoint-execution-contract@1"
                            ),
                            "entrypoint_evidence": identities,
                        }
                    ),
                )
                with self.assertRaisesRegex(
                    QualificationCaptureError, "execution-command-mismatch"
                ):
                    verify_qualification_execution(
                        reader(), build_plan_identity=plan.identity, execution=forged
                    )
            original = reader().read_json(execution.stdout_identity)
            changed = dict(original)
            changed[next(iter(changed))] = "foreign output"
            forged = recorder.remember_json(changed)
            with self.assertRaisesRegex(
                QualificationCaptureError, "execution-process-mismatch"
            ):
                verify_qualification_execution(
                    reader(),
                    build_plan_identity=plan.identity,
                    execution=replace(execution, stdout_identity=forged),
                )

    def test_library_consumer_binding_matches_exact_edge_and_artifact(self) -> None:
        lock = _diamond_lock()
        execution = plan_component_execution(lock, model_identities=_models(lock))
        generation_plan = next(
            item for item in execution.generation_plans if item.direct_generation_edges
        )
        edge = generation_plan.direct_generation_edges[0]
        assert edge.public_interface_identity is not None
        binding = LocalComponentToolBinding(sys.executable)
        commands = tuple(
            ComponentLifecycleCommand(
                phase,
                (
                    "{tool}",
                    "phase",
                    "{source_root}"
                    if phase is ComponentCommandPhase.BUILD
                    else "{artifact_root}",
                    "{object_root}"
                    if phase is ComponentCommandPhase.BUILD
                    else "fixed",
                    "{export_path}"
                    if phase is ComponentCommandPhase.BUILD
                    else "fixed2",
                ),
            )
            for phase in ComponentCommandPhase
        )
        provider_shape = ComponentArtifactExportShape(
            "provider-library",
            "library",
            _identity("provider-abi"),
            _identity("shared-target"),
            "application/vnd.literate-ai.python-package-tree",
            _identity("provider-producer"),
        )
        provider_contract = ComponentCommandContract(
            edge.provider_revision,
            _identity("provider-build"),
            _identity("provider-resolver"),
            binding.toolchain_identity,
            binding.toolchain_identity,
            binding.toolchain_identity,
            commands,
            tuple(
                ComponentCommandToolBinding(phase, binding.toolchain_identity)
                for phase in ComponentCommandPhase
            ),
            provider_shape,
            library_import_surface=LibraryImportSurface(
                "python",
                "provider",
                (
                    LibraryCapabilityImport(
                        edge.capability,
                        edge.public_interface_identity,
                        "provider.capability",
                        ("capability",),
                    ),
                ),
            ),
        )
        consumer_contract = ComponentCommandContract(
            generation_plan.component_revision,
            _identity("consumer-build"),
            _identity("consumer-resolver"),
            binding.toolchain_identity,
            binding.toolchain_identity,
            binding.toolchain_identity,
            commands,
            tuple(
                ComponentCommandToolBinding(phase, binding.toolchain_identity)
                for phase in ComponentCommandPhase
            ),
            ComponentArtifactExportShape(
                "consumer",
                "executable",
                _identity("consumer-abi"),
                _identity("shared-target"),
                "application/octet-stream",
                _identity("consumer-producer"),
            ),
        )
        payload = b"sealed-library"
        artifact = ArtifactExport(
            provider_shape.export_id,
            edge.provider_revision,
            provider_shape.role,
            provider_shape.abi_identity,
            provider_shape.target_identity,
            provider_shape.media_type,
            provider_shape.producer_identity,
            _identity("provider-source"),
            binding.toolchain_identity,
            _identity("provider-authorization"),
            (),
            BlobRef(
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                media_type=provider_shape.media_type,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(consumer_contract, provider_contract),
                tool_bindings=(binding,),
            )
            candidate = SimpleNamespace(
                component_revision=generation_plan.component_revision,
                component_generation_plan_identity=generation_plan.identity,
                tree_identity=_identity("consumer-tree"),
                source_bundle_identity=_identity("consumer-bundle"),
            )
            intent = ports.create(
                execution, generation_plan, candidate, (artifact,), ()
            )
            exact = ports.library_consumer_bindings(intent)
            materials = ports._provider_materials(
                (artifact,), consumer_revision=generation_plan.component_revision
            )
            registered = (
                dict(ports._intent_artifacts),
                dict(ports._intent_package_artifacts),
                dict(ports._library_consumer_bindings),
            )
            with self.assertRaisesRegex(LocalStandardLifecycleError, "target differs"):
                ports.create(
                    execution,
                    generation_plan,
                    candidate,
                    (replace(artifact, target_identity=_identity("wrong-target")),),
                    (),
                )
            self.assertEqual(
                registered,
                (
                    ports._intent_artifacts,
                    ports._intent_package_artifacts,
                    ports._library_consumer_bindings,
                ),
            )

        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0].artifact_identity, artifact.identity)
        self.assertEqual(exact[0].capability, edge.capability)
        self.assertEqual(exact[0].interface_identity, edge.public_interface_identity)
        self.assertEqual(
            materials[0]["library_consumer_bindings"], [exact[0].to_dict()]
        )

    def test_library_build_test_execute_and_accept_without_entrypoint(self) -> None:
        snapshot, execution = _fixture()
        generation_plan = next(
            plan
            for plan in execution.generation_plans
            if plan.component_revision == snapshot.authority.lock.root_revision
        )
        binding = LocalComponentToolBinding(sys.executable)
        surface = LibraryImportSurface(
            "python",
            "sample_library",
            (
                LibraryCapabilityImport(
                    "sample.portable-app",
                    _identity("library-interface"),
                    "sample_library.logic",
                    ("portable_app",),
                ),
            ),
        )
        commands = (
            ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                (
                    "{tool}",
                    "-c",
                    "import shutil,sys;shutil.copytree(sys.argv[1],sys.argv[2])",
                    "{source_root}",
                    "{export_path}",
                    "{object_root}",
                ),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.TEST,
                (
                    "{tool}",
                    "-c",
                    "import json;print(json.dumps(dict(schema="
                    "'literate-ai/generated-test-results@1',cases=["
                    "dict(case_id='fixture-example',outcome='passed'),"
                    "dict(case_id='fixture-boundary',outcome='passed'),"
                    "dict(case_id='fixture-invariant',outcome='passed')])))",
                    "{artifact_root}",
                ),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.EXECUTE,
                (
                    "{tool}",
                    "-c",
                    "import json;print(json.dumps(dict(imported=True)))",
                    "{artifact_root}",
                ),
            ),
        )
        contract = ComponentCommandContract(
            component_revision=generation_plan.component_revision,
            locked_build_authority_identity=_identity("library-build-authority"),
            build_system_resolver_identity=_identity("library-resolver"),
            build_system_toolchain_identity=binding.toolchain_identity,
            language_compiler_identity=binding.toolchain_identity,
            language_runtime_identity=binding.toolchain_identity,
            commands=commands,
            tool_bindings=tuple(
                ComponentCommandToolBinding(phase, binding.toolchain_identity)
                for phase in ComponentCommandPhase
            ),
            artifact_export=ComponentArtifactExportShape(
                "library",
                "library",
                _identity("library-abi"),
                _identity("target"),
                "application/vnd.literate-ai.python-package-tree",
                _identity("producer"),
            ),
            library_import_surface=surface,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            source.joinpath("sample_library.py").write_text(
                "portable_app = 7\n", encoding="utf-8"
            )
            registry = LocalSourceTreeRegistry()
            candidate = register_strict_source(
                registry,
                source,
                snapshot=snapshot,
                generation_plan=generation_plan,
                identity_namespace="standard-library-command-test",
            )
            ports = LocalStandardLifecyclePorts(
                source_trees=registry,
                object_root=root / "objects",
                contracts=(contract,),
                tool_bindings=(binding,),
            )
            recorder = QualificationEvidenceRecorder(
                max_bytes=1_000_000, max_records=200
            )
            ports.retain_evidence_with(recorder)
            intent = ports.create(execution, generation_plan, candidate, (), ())
            index = ports.index(candidate.component_revision, candidate.tree_identity)
            plan = ports.finalize(intent, ports.authorize(intent, index))
            verify_qualification_command_authority(
                QualificationEvidenceReader(
                    recorder.entries, max_bytes=1_000_000, max_records=200
                ),
                contract=contract,
                authorization_identity=plan.request.authorization_identity,
                plan=plan,
            )
            built = ports.build(plan, ())
            tests = ports.test(plan, built.exports)
            executed = ports.execute(plan, built.exports)
            accepted = ports.accept(plan, tests.identity, executed.identity)
            manifest = realize_manifest(plan.manifest, built.exports)
            graph = create_artifact_build_graph(
                build_system_driver_identity=manifest.build_system_driver_identity,
                manifests=(manifest,),
                link_roots=(built.exports[0].identity,),
            )
            package_plan, package_result = ports.create_project_package(
                snapshot.authority.lock,
                execution,
                SimpleNamespace(),
                graph,
                graph.link_plans[0],
            )

            self.assertTrue(ports.artifact_path(built.exports[0]).is_dir())
            self.assertEqual(len(tests.cases), 3)
            self.assertEqual(accepted.build.identity, built.evidence.identity)
            self.assertEqual(package_plan.package_kind.value, "directory")
            self.assertEqual(package_plan.entrypoints, ())
            self.assertEqual(package_result.entrypoints, ())
            self.assertEqual(package_plan.runtime_requirements, ())
            with self.assertRaises(ContractValidationError):
                contract.entrypoint_command_contracts()

    def test_provider_environment_values_binds_declared_artifact_export_providers(
        self,
    ) -> None:
        """#110: a declared artifact-export (build/runtime) provider resolves."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                provider_environment={
                    "artifact-money": ("LITAI_PROVIDER_MONEY", "manifest.json")
                },
            )
            export = _provider_export("artifact-money")
            artifact_root = root / "money-artifact"
            artifact_root.mkdir()
            (artifact_root / "manifest.json").write_bytes(b"{}")
            ports._artifact_paths[export.identity.uri] = artifact_root

            environment = ports._provider_environment_values((export,))

            self.assertEqual(
                environment,
                {
                    "LITAI_PROVIDER_MONEY": str(
                        (artifact_root / "manifest.json").resolve()
                    )
                },
            )

    def test_provider_environment_values_fails_closed_without_a_declared_binding(
        self,
    ) -> None:
        """#110: an undeclared provider (e.g. packaging-only) must never be resolved.

        ``_provider_environment_values`` only ever sees the providers its caller
        chose to pass in. This is the low-level guard: whatever reaches here without
        a declared runtime binding fails closed rather than silently proceeding --
        it is the caller's responsibility (issue #110's actual fix) to never pass a
        packaging-only provider into this path in the first place.
        """

        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary) / "objects",
                contracts=(),
                provider_environment={},
            )
            export = _provider_export("artifact-money")
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "has no runtime binding"
            ):
                ports._provider_environment_values((export,))

    def test_project_acceptance_fails_closed_without_verifier_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
            )
            plan = SimpleNamespace(
                entrypoints=(SimpleNamespace(kind="portable-application"),)
            )
            with (
                mock.patch.object(
                    ports, "project_package_custody", return_value=object()
                ),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError, "verifier-owned oracle"
                ),
            ):
                ports.accept_project_independently(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    mock.sentinel.package_result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )

    def test_project_acceptance_exempts_non_portable_application_entrypoints(
        self,
    ) -> None:
        """#32: a non-single-shot entrypoint needs no hand-authored oracle at all."""

        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
            )
            plan = SimpleNamespace(
                identity=_identity("service-package-plan"),
                entrypoints=(SimpleNamespace(kind="service"),),
            )
            with mock.patch.object(ports, "project_package_custody") as custody:
                evidence = ports.accept_project_independently(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    mock.sentinel.package_result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )
            # No oracle file, no packaged command invocation -- acceptance for a
            # non-portable-application entrypoint must never touch package custody.
            custody.assert_not_called()
            self.assertIsInstance(evidence, type(_identity("x")))
            again = ports.accept_project_independently(
                mock.sentinel.component_lock,
                mock.sentinel.execution_plan,
                mock.sentinel.project_build_plan,
                plan,
                mock.sentinel.package_result,
                _identity("root-test"),
                _identity("package-execution"),
            )
            self.assertEqual(evidence, again)

    def test_packaged_execute_is_deferred_for_persistent_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
            )
            plan = SimpleNamespace(
                identity=_identity("service-package-plan"),
                entrypoints=(SimpleNamespace(kind="persistent-service"),),
            )
            result = SimpleNamespace(identity=_identity("service-package-result"))
            with mock.patch.object(ports, "project_package_custody") as custody:
                evidence = ports.execute_packaged_project(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    result,
                )
            custody.assert_not_called()
            self.assertEqual(
                evidence,
                ports.execute_packaged_project(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    result,
                ),
            )

    def test_persistent_service_acceptance_fails_closed_without_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
            )
            plan = SimpleNamespace(
                identity=_identity("service-package-plan"),
                entrypoints=(SimpleNamespace(kind="persistent-service"),),
            )
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "verifier-owned contract"
            ):
                ports.accept_project_independently(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    mock.sentinel.package_result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )

    def test_persistent_service_exit_before_readiness_is_structured(self) -> None:
        """#333: an early service exit must carry bounded child diagnostics."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = root / "package"
            package_root.mkdir()
            leak = package_root / "secret-config.json"
            leak.write_text("token\n", encoding="utf-8")
            service = package_root / "service.py"
            diagnostic = f"CONFIG_REQUIRED missing {leak}\n"
            service.write_text(
                textwrap.dedent(
                    f"""
                    import sys
                    sys.stderr.write({diagnostic!r})
                    sys.stderr.flush()
                    raise SystemExit(7)
                    """
                ),
                encoding="utf-8",
            )
            contract_path = root / "acceptance.json"
            contract_path.write_text(
                json.dumps(
                    {
                        "schema": SERVICE_SCHEMA,
                        "specification_set_identity": "sha256:spec",
                        "process": {"arguments": ["{port}"]},
                        "readiness": {
                            "path": "/ready",
                            "expected_text_contains": ["ready"],
                        },
                        "requests": [
                            {"path": "/value", "expected_json": {"value": 7}},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            contract = load_persistent_service_acceptance(contract_path, "service")
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=contract,
            )
            custody = SimpleNamespace(
                root=package_root, tree_identity=local_tree_identity(package_root)
            )
            plan = SimpleNamespace(
                identity=_identity("service-plan"),
                entrypoints=(SimpleNamespace(kind="persistent-service"),),
            )
            result = SimpleNamespace(identity=_identity("service-result"))
            lock = mock.Mock()
            lock.root_revision = _identity("root")
            lock.nodes = (
                SimpleNamespace(
                    revision=SimpleNamespace(
                        identity=lock.root_revision,
                        specification_set_identity=SimpleNamespace(uri="sha256:spec"),
                    )
                ),
            )
            with (
                mock.patch.object(
                    ports, "project_package_custody", return_value=custody
                ),
                mock.patch.object(
                    ports,
                    "_packaged_argv",
                    return_value=(sys.executable, str(service), "--litai-smoke"),
                ),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
            ):
                with self.assertRaises(LocalStandardLifecycleError) as raised:
                    ports.accept_project_independently(
                        lock,
                        mock.sentinel.execution_plan,
                        mock.sentinel.project_build_plan,
                        plan,
                        result,
                        _identity("root-test"),
                        _identity("package-execution"),
                    )
            error = raised.exception
            self.assertEqual(error.code, "lifecycle.persistent-service.exited")
            self.assertEqual(error.returncode, 7)
            self.assertEqual(error.phase, "readiness")
            self.assertIn("CONFIG_REQUIRED", error.message)
            self.assertNotIn(str(leak), error.message)
            self.assertNotIn("Traceback", error.message)

    @unittest.skipIf(
        sys.platform == "darwin",
        "0.6.0: persistent-service readiness probe times out on macOS GitHub runners",
    )
    def test_persistent_service_acceptance_runs_http_and_sse_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = root / "package"
            package_root.mkdir()
            marker = root / "stopped"
            service = package_root / "service.py"
            service.write_text(
                textwrap.dedent(
                    """
                    import json, signal, sys
                    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
                    from pathlib import Path

                    # Framework-owned serve dispatcher: the runtime driver passes
                    # --litai-serve as the trailing mode token, followed by the
                    # verifier-owned arguments (port, marker). A real generated
                    # service recognizes this mode, binds, and stays alive (#214).
                    assert sys.argv[1] == '--litai-serve', sys.argv
                    port, marker = int(sys.argv[2]), Path(sys.argv[3])
                    class Handler(BaseHTTPRequestHandler):
                        def do_GET(self):
                            if self.path == '/ready':
                                body = b'ready'
                                content_type = 'text/plain'
                            elif self.path == '/events':
                                body = b'event: state\\ndata: connected\\n\\n'
                                content_type = 'text/event-stream'
                            else:
                                body = json.dumps({'value': 7}).encode()
                                content_type = 'application/json'
                            self.send_response(200)
                            self.send_header('Content-Type', content_type)
                            self.end_headers()
                            self.wfile.write(body)
                        def log_message(self, *args):
                            pass
                    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
                    def stop(*args):
                        marker.write_text('stopped')
                        raise KeyboardInterrupt
                    signal.signal(signal.SIGTERM, stop)
                    if hasattr(signal, 'SIGBREAK'):
                        signal.signal(signal.SIGBREAK, stop)
                    try:
                        server.serve_forever()
                    except KeyboardInterrupt:
                        pass
                    """
                ),
                encoding="utf-8",
            )
            contract_path = root / "acceptance.json"
            contract_path.write_text(
                json.dumps(
                    {
                        "schema": SERVICE_SCHEMA,
                        "specification_set_identity": "sha256:spec",
                        "process": {
                            "arguments": ["{port}", str(marker)],
                            "environment": {"SERVICE_BASE_URL": "{base_url}"},
                        },
                        "readiness": {
                            "path": "/ready",
                            "expected_text_contains": ["ready"],
                        },
                        "requests": [
                            {"path": "/value", "expected_json": {"value": 7}},
                            {
                                "path": "/events",
                                "expected_headers": {
                                    "Content-Type": "text/event-stream"
                                },
                                "expected_sse_events": [
                                    {"event": "state", "data": "connected"}
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            contract = load_persistent_service_acceptance(contract_path, "service")
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=contract,
            )
            custody = SimpleNamespace(
                root=package_root, tree_identity=local_tree_identity(package_root)
            )
            plan = SimpleNamespace(
                identity=_identity("service-plan"),
                entrypoints=(SimpleNamespace(kind="persistent-service"),),
            )
            result = SimpleNamespace(identity=_identity("service-result"))
            lock = mock.Mock()
            lock.root_revision = _identity("root")
            lock.nodes = (
                SimpleNamespace(
                    revision=SimpleNamespace(
                        identity=lock.root_revision,
                        specification_set_identity=SimpleNamespace(uri="sha256:spec"),
                    )
                ),
            )
            with (
                mock.patch.object(
                    ports, "project_package_custody", return_value=custody
                ),
                mock.patch.object(
                    ports,
                    "_packaged_argv",
                    # Model the packaged persistent-service EXECUTE argv, whose
                    # trailing one-shot --litai-smoke mode _packaged_service_argv
                    # rewrites to --litai-serve for the served launch (#214).
                    return_value=(sys.executable, str(service), "--litai-smoke"),
                ),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
            ):
                evidence = ports.accept_project_independently(
                    lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )
            self.assertIsNotNone(evidence)
            self.assertEqual(marker.read_text(encoding="utf-8"), "stopped")

    @unittest.skipUnless(
        sys.platform.startswith("linux") and shutil.which("node"),
        "real JavaScript service regression runs on a Linux host with Node",
    )
    def test_javascript_persistent_service_acceptance_owns_server_process(
        self,
    ) -> None:
        """#247: readiness observes the server PID, never a live wrapper."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = root / "package"
            package_root.mkdir()
            marker = root / "stopped"
            service = package_root / "service.js"
            service.write_text(
                textwrap.dedent(
                    """
                    const fs = require('fs');
                    const http = require('http');
                    if (process.argv[2] !== '--litai-serve') process.exit(41);
                    const port = Number(process.argv[3]);
                    const marker = process.argv[4];
                    const server = http.createServer((request, response) => {
                      response.end(request.url === '/ready' ? 'ready' : 'ok');
                    });
                    const stop = () => server.close(() => {
                      fs.writeFileSync(marker, 'stopped');
                      process.exit(0);
                    });
                    process.on('SIGTERM', stop);
                    process.on('SIGINT', stop);
                    server.listen(port, '127.0.0.1');
                    """
                ),
                encoding="utf-8",
            )
            contract_path = root / "acceptance.json"
            contract_path.write_text(
                json.dumps(
                    {
                        "schema": SERVICE_SCHEMA,
                        "specification_set_identity": "sha256:spec",
                        "process": {"arguments": ["{port}", str(marker)]},
                        "readiness": {
                            "path": "/ready",
                            "expected_text_contains": ["ready"],
                        },
                        "requests": [
                            {
                                "path": "/value",
                                "expected_text_contains": ["ok"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            contract = load_persistent_service_acceptance(contract_path, "service")
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=contract,
            )
            custody = SimpleNamespace(
                root=package_root, tree_identity=local_tree_identity(package_root)
            )
            plan = SimpleNamespace(identity=_identity("javascript-service-plan"))
            result = SimpleNamespace(identity=_identity("javascript-service-result"))
            node = shutil.which("node")
            assert node is not None
            with (
                mock.patch.object(
                    ports,
                    "_packaged_argv",
                    return_value=(
                        node,
                        "-e",
                        STANDARD_NODE_RUNTIME_DRIVER,
                        str(package_root),
                        str(service),
                        "file",
                        "source/main.js",
                        "--litai-smoke",
                    ),
                ),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
            ):
                evidence = ports._accept_persistent_service(
                    custody,
                    plan,
                    result,
                    contract,
                    _identity("javascript-root-test"),
                    _identity("javascript-package-execution"),
                )

            self.assertIsNotNone(evidence)
            self.assertEqual(marker.read_text(encoding="utf-8"), "stopped")

    def test_web_application_dispatch_drives_the_browser_port_and_stops(self) -> None:
        # ADR-0028: a web-application entrypoint launches the served frontend via
        # --litai-serve (same as a persistent service) and drives a browser-port
        # driver against it. Here a scripted fake driver stands in for Playwright,
        # so the dispatch + launch + shutdown + evidence receipt are proven without
        # a real browser. The evidence binds viewport, contract, tool identity,
        # screenshots, console failures, and outcome.
        from literate_ai.adapters.browser_acceptance import (
            BrowserConsoleFailure,
            BrowserObservation,
            BrowserPageDriver,
        )
        from literate_ai.adapters.component_acceptance import (
            BROWSER_SCHEMA,
            load_browser_interaction_acceptance,
        )

        class FakeDriver(BrowserPageDriver):
            def __init__(self) -> None:
                self.seen: list[str] = []

            @property
            def tool_identity(self):
                return canonical_identity(
                    {"schema": "literate-ai/browser-driver@1", "engine": "fake"}
                )

            def observe(self, base_url, viewport, contract):
                self.seen.append(viewport.label)
                return BrowserObservation(
                    viewport_width=viewport.width,
                    viewport_height=viewport.height,
                    document_scroll_width=viewport.width,
                    console_failures=(
                        BrowserConsoleFailure("console-warning", "diagnostic"),
                    ),
                    request_count=0,
                    resolved_postconditions=(("row-1", True),),
                    observed_landmarks=("main",),
                    screenshot=b"png",
                    accessibility_snapshot=b"{}",
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = root / "package"
            package_root.mkdir()
            marker = root / "stopped"
            service = package_root / "frontend.py"
            service.write_text(
                textwrap.dedent(
                    """
                    import signal, sys
                    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
                    from pathlib import Path

                    assert sys.argv[1] == '--litai-serve', sys.argv
                    port, marker = int(sys.argv[2]), Path(sys.argv[3])
                    class Handler(BaseHTTPRequestHandler):
                        def do_GET(self):
                            self.send_response(200)
                            self.end_headers()
                            self.wfile.write(b'ok')
                        def log_message(self, *args):
                            pass
                    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
                    def stop(*args):
                        marker.write_text('stopped')
                        raise KeyboardInterrupt
                    signal.signal(signal.SIGTERM, stop)
                    if hasattr(signal, 'SIGBREAK'):
                        signal.signal(signal.SIGBREAK, stop)
                    try:
                        server.serve_forever()
                    except KeyboardInterrupt:
                        pass
                    """
                ),
                encoding="utf-8",
            )
            contract_path = root / "acceptance.json"
            contract_path.write_text(
                json.dumps(
                    {
                        "schema": BROWSER_SCHEMA,
                        "specification_set_identity": "sha256:spec",
                        "process": {"arguments": ["{port}", str(marker)]},
                        "readiness_path": "/health",
                        "viewports": [
                            {"label": "desktop", "width": 1280, "height": 800},
                            {"label": "mobile", "width": 390, "height": 844},
                        ],
                        "postconditions": [
                            {
                                "postcondition_id": "row-1",
                                "kind": "row-identity",
                                "selector": ".row",
                                "expected": "Row 1",
                            }
                        ],
                        "required_landmarks": ["main"],
                        "rejection_classes": ["console-error", "page-error"],
                        "expected_request_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            contract = load_browser_interaction_acceptance(contract_path, "frontend")
            driver = FakeDriver()
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=contract,
                browser_driver=driver,
            )
            custody = SimpleNamespace(
                root=package_root, tree_identity=local_tree_identity(package_root)
            )
            plan = SimpleNamespace(
                identity=_identity("frontend-plan"),
                entrypoints=(SimpleNamespace(kind="web-application"),),
            )
            result = SimpleNamespace(identity=_identity("frontend-result"))
            lock = mock.Mock()
            lock.root_revision = _identity("root")
            lock.nodes = (
                SimpleNamespace(
                    revision=SimpleNamespace(
                        identity=lock.root_revision,
                        specification_set_identity=SimpleNamespace(uri="sha256:spec"),
                    )
                ),
            )
            process = mock.Mock(pid=7319)
            process.poll.return_value = None
            ownership = mock.Mock(popen_options={})
            with (
                mock.patch.object(
                    ports, "project_package_custody", return_value=custody
                ),
                mock.patch.object(
                    ports,
                    "_packaged_argv",
                    return_value=(sys.executable, str(service), "--litai-smoke"),
                ),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local."
                    "create_process_tree_ownership",
                    return_value=ownership,
                ),
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.subprocess.Popen",
                    return_value=process,
                ) as popen,
                mock.patch.object(
                    ports,
                    "_frontend_is_ready",
                    side_effect=(False, False, True),
                ) as readiness,
                mock.patch.object(ports, "_stop_service_process") as stop,
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.time.sleep"
                ) as sleep,
            ):
                evidence = ports.accept_project_independently(
                    lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )
            self.assertIsNotNone(evidence)
            # Both declared viewports were driven, and the fake tool identity is
            # bound into the receipt distinctly from the interaction contract.
            self.assertEqual(driver.seen, ["desktop", "mobile"])
            self.assertEqual(readiness.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            command = popen.call_args.args[0]
            self.assertEqual(
                command[:3], (sys.executable, str(service), "--litai-serve")
            )
            self.assertTrue(command[3].isdigit())
            self.assertEqual(command[4], str(marker))
            stop.assert_called_once()
            self.assertIs(stop.call_args.args[0], process)
            ownership.bind.assert_called_once_with(process.pid)
            ownership.release.assert_called_once_with()

            timed_out_process = mock.Mock(pid=7320)
            timed_out_process.poll.return_value = None
            timed_out_ownership = mock.Mock(popen_options={})
            with (
                mock.patch.object(
                    ports, "project_package_custody", return_value=custody
                ),
                mock.patch.object(
                    ports,
                    "_packaged_argv",
                    return_value=(sys.executable, str(service), "--litai-smoke"),
                ),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local."
                    "create_process_tree_ownership",
                    return_value=timed_out_ownership,
                ),
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.subprocess.Popen",
                    return_value=timed_out_process,
                ),
                mock.patch.object(ports, "_frontend_is_ready", return_value=False),
                mock.patch.object(ports, "_stop_service_process") as timed_out_stop,
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.time.monotonic",
                    side_effect=(
                        0.0,
                        0.0,
                        contract.startup_timeout_seconds + 1.0,
                    ),
                ),
                mock.patch("literate_ai.adapters.lifecycle.standard_local.time.sleep"),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError,
                    "readiness probe did not pass before its deadline",
                ),
            ):
                ports.accept_project_independently(
                    lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )
            timed_out_stop.assert_called_once()
            self.assertIs(timed_out_stop.call_args.args[0], timed_out_process)
            timed_out_ownership.bind.assert_called_once_with(timed_out_process.pid)
            timed_out_ownership.release.assert_called_once_with()

    def test_web_application_dispatch_fails_closed_without_a_browser_contract(
        self,
    ) -> None:
        # A web-application entrypoint with a non-browser oracle must fail closed,
        # never fall through to the text/HTTP path.
        ports = LocalStandardLifecyclePorts(
            source_trees=LocalSourceTreeRegistry(),
            object_root=Path(tempfile.mkdtemp()) / "objects",
            contracts=(),
            independent_acceptance_oracle=object(),
        )
        plan = SimpleNamespace(
            identity=_identity("frontend-plan"),
            entrypoints=(SimpleNamespace(kind="web-application"),),
        )
        result = SimpleNamespace(identity=_identity("frontend-result"))
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "verifier-owned contract"
        ):
            ports.accept_project_independently(
                mock.Mock(),
                mock.sentinel.execution_plan,
                mock.sentinel.project_build_plan,
                plan,
                result,
                _identity("root-test"),
                _identity("package-execution"),
            )

    def test_child_process_environment_fills_host_essentials(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/host/bin",
                "HOME": "/host/home",
                "SYSTEMROOT": "C:\\Windows",
                "SECRET_TOKEN": "must-not-copy",
            },
            clear=False,
        ):
            environment = _child_process_environment({"SERVICE_BASE_URL": "http://x"})
        self.assertEqual(environment["PATH"], "/host/bin")
        self.assertEqual(environment["HOME"], "/host/home")
        self.assertEqual(environment["SYSTEMROOT"], "C:\\Windows")
        self.assertEqual(environment["SERVICE_BASE_URL"], "http://x")
        self.assertNotIn("SECRET_TOKEN", environment)

    def test_child_process_environment_lets_packaged_values_win(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/host/bin"}, clear=False):
            environment = _child_process_environment({"PATH": "/package/bin"})
        self.assertEqual(environment["PATH"], "/package/bin")

    def test_host_process_environment_does_not_invent_c_utf8_on_darwin(self) -> None:
        with (
            mock.patch(
                "literate_ai.adapters.lifecycle.standard_local.sys"
            ) as sys_module,
            mock.patch.dict(os.environ, {"PATH": "/bin", "HOME": "/h"}, clear=True),
        ):
            sys_module.platform = "darwin"
            darwin_environment = _host_process_environment()
            sys_module.platform = "linux"
            linux_environment = _host_process_environment()
        self.assertNotIn("LC_ALL", darwin_environment)
        if os.name != "nt":
            self.assertEqual(linux_environment.get("LC_ALL"), "C.UTF-8")

    def test_persistent_service_acceptance_rejects_specification_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": SERVICE_SCHEMA,
                        "specification_set_identity": "sha256:old",
                        "process": {},
                        "readiness": {"path": "/ready"},
                        "requests": [{"path": "/value"}],
                    }
                ),
                encoding="utf-8",
            )
            contract = load_persistent_service_acceptance(path, "service")
            lock = mock.Mock()
            lock.root_revision = _identity("root")
            lock.nodes = (
                SimpleNamespace(
                    revision=SimpleNamespace(
                        identity=lock.root_revision,
                        specification_set_identity=SimpleNamespace(uri="sha256:new"),
                    )
                ),
            )
            with self.assertRaisesRegex(ComponentAcceptanceError, "review and rebind"):
                contract.require_current(lock)

    def test_persistent_service_http_rejects_mismatch_and_overflow(self) -> None:
        from literate_ai.adapters.component_acceptance import ServiceHttpProbe

        observations: list[dict[str, object]] = []
        mismatch = ServiceHttpProbe(
            "GET", "/value", (), None, 201, (), None, (), (), 1, 1024
        )
        overflow = ServiceHttpProbe(
            "GET", "/value", (), None, 200, (), None, (), (), 1, 1
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers.items.return_value = ()
        response.read.return_value = b"long"
        with (
            mock.patch(
                "literate_ai.adapters.lifecycle.standard_local.open_service_request",
                return_value=response,
            ) as opened,
            self.assertRaisesRegex(LocalStandardLifecycleError, "byte budget"),
        ):
            LocalStandardLifecyclePorts._observe_service_request(
                overflow,
                "http://127.0.0.1:1",
                observations,
                timeout_seconds=0.25,
            )
        self.assertEqual(opened.call_args.kwargs["timeout"], 0.25)
        response.read.return_value = b"ok"
        with (
            mock.patch(
                "literate_ai.adapters.lifecycle.standard_local.open_service_request",
                return_value=response,
            ),
            self.assertRaisesRegex(LocalStandardLifecycleError, "status differed"),
        ):
            LocalStandardLifecyclePorts._observe_service_request(
                mismatch, "http://127.0.0.1:1", observations
            )
        binary = ServiceHttpProbe(
            "GET", "/value", (), None, 200, (), None, (), (), 1, 1024
        )
        response.read.return_value = b"\xff"
        with mock.patch(
            "literate_ai.adapters.lifecycle.standard_local.open_service_request",
            return_value=response,
        ):
            LocalStandardLifecyclePorts._observe_service_request(
                binary, "http://127.0.0.1:1", observations
            )

    def test_service_http_product_json_accepts_finite_and_rejects_nonfinite(self):
        from literate_ai.adapters.component_acceptance import ServiceHttpProbe
        from literate_ai.contracts.product_json import product_json_identity

        probe = ServiceHttpProbe(
            "GET", "/value", (), None, 200, (), {"value": 4.5}, (), (), 1, 1024, True
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers.items.return_value = ()
        observations = []
        with mock.patch(
            "literate_ai.adapters.lifecycle.standard_local.open_service_request",
            return_value=response,
        ):
            response.read.return_value = b'{"value":4.5}'
            LocalStandardLifecyclePorts._observe_service_request(
                probe, "http://127.0.0.1:1", observations
            )
            self.assertEqual(
                observations[0]["result_identity"],
                product_json_identity({"value": 4.5}).uri,
            )
            for value in (b"NaN", b"Infinity", b"-Infinity"):
                response.read.return_value = b'{"value":' + value + b"}"
                with (
                    self.subTest(value=value),
                    self.assertRaisesRegex(
                        LocalStandardLifecycleError, "not finite JSON"
                    ),
                ):
                    LocalStandardLifecyclePorts._observe_service_request(
                        probe, "http://127.0.0.1:1", observations
                    )
        self.assertEqual(len(observations), 1)

    def test_service_http_compares_exact_values_and_retains_observed_bytes(self):
        from literate_ai.adapters.component_acceptance import ServiceHttpProbe
        from literate_ai.contracts.product_json import product_json_identity

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers.items.return_value = ()
        for actual, expected, accepted in (
            (1, 1.0, True),
            ([0.0, {"n": 2}], [0, {"n": 2.0}], True),
            (True, 1, False),
            (False, 0.0, False),
            (-0.0, 0, False),
            (float(2**53 + 1), 2**53 + 1, False),
            (1.0, 1.0000000000000002, False),
        ):
            with self.subTest(actual=actual, expected=expected):
                probe = ServiceHttpProbe(
                    "GET", "/value", (), None, 200, (), expected, (), (), 1, 1024, True
                )
                response.read.return_value = json.dumps(actual).encode("utf-8")
                observations = []
                with mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.open_service_request",
                    return_value=response,
                ):
                    if accepted:
                        LocalStandardLifecyclePorts._observe_service_request(
                            probe, "http://127.0.0.1:1", observations
                        )
                        self.assertEqual(
                            observations[0]["result_identity"],
                            product_json_identity(actual).uri,
                        )
                        self.assertNotEqual(
                            observations[0]["result_identity"],
                            product_json_identity(expected).uri,
                        )
                    else:
                        with self.assertRaisesRegex(
                            LocalStandardLifecycleError, "JSON response differed"
                        ):
                            LocalStandardLifecyclePorts._observe_service_request(
                                probe, "http://127.0.0.1:1", observations
                            )
                        self.assertEqual(observations, [])

    def test_persistent_service_contract_can_expect_json_null(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": SERVICE_SCHEMA,
                        "specification_set_identity": "sha256:spec",
                        "process": {},
                        "readiness": {"path": "/ready"},
                        "requests": [{"path": "/value", "expected_json": None}],
                    }
                ),
                encoding="utf-8",
            )
            contract = load_persistent_service_acceptance(path, "service")
        probe = contract.requests[0]
        self.assertTrue(probe.expected_json_present)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers.items.return_value = ()
        response.read.return_value = b"null"
        with mock.patch(
            "literate_ai.adapters.lifecycle.standard_local.open_service_request",
            return_value=response,
        ):
            LocalStandardLifecyclePorts._observe_service_request(
                probe, "http://127.0.0.1:1", []
            )

    def test_persistent_service_contract_rejects_unbounded_or_unknown_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": SERVICE_SCHEMA,
                        "specification_set_identity": "sha256:spec",
                        "process": {"timeout_seconds": 301},
                        "readiness": {"path": "/ready"},
                        "requests": [{"path": "/value", "surprise": True}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ComponentAcceptanceError):
                load_persistent_service_acceptance(path, "service")

    def test_project_acceptance_requires_a_multi_entrypoint_oracle_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
            )
            plan = SimpleNamespace(
                entrypoints=(
                    SimpleNamespace(kind="portable-application"),
                    SimpleNamespace(kind="portable-application"),
                )
            )
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "verifier-owned oracle bundle"
            ):
                ports.accept_project_independently(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    mock.sentinel.package_result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )

    def test_project_acceptance_exempts_library_only_components(self) -> None:
        """#114: a library-only Component has no packaged entrypoint at all."""

        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
            )
            plan = SimpleNamespace(
                identity=_identity("library-package-plan"), entrypoints=()
            )
            with mock.patch.object(ports, "project_package_custody") as custody:
                evidence = ports.accept_project_independently(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    mock.sentinel.package_result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )
            custody.assert_not_called()
            self.assertIsInstance(evidence, type(_identity("x")))

    def test_project_acceptance_compares_with_verifier_known_result(self) -> None:
        class Oracle:
            identity = _identity("independent-oracle")

            def cases(self, component_lock):
                del component_lock
                return (
                    LocalIndependentAcceptanceCase.create(
                        "known-result", [], {"value": 1}
                    ),
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package-marker").write_bytes(b"immutable-package")
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=Oracle(),
            )
            custody = SimpleNamespace(
                root=root,
                tree_identity=local_tree_identity(root),
                native_sdk_resources=None,
            )
            plan = SimpleNamespace(
                identity=_identity("package-plan"),
                entrypoints=(SimpleNamespace(kind="portable-application"),),
            )
            result = SimpleNamespace(identity=_identity("package-result"))
            command = (
                sys.executable,
                "-c",
                "import json; print(json.dumps({'value': 2}))",
            )
            with (
                mock.patch.object(
                    ports, "project_package_custody", return_value=custody
                ),
                mock.patch.object(ports, "_packaged_argv", return_value=command),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError, "differs for 'known-result'"
                ) as caught,
            ):
                ports.accept_project_independently(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )
            message = str(caught.exception)
            self.assertIn(canonical_identity({"value": 1}).uri, message)
            self.assertIn(canonical_identity({"value": 2}).uri, message)
            self.assertIn(Oracle.identity.uri, message)

    def test_project_acceptance_passes_one_canonical_json_argument_array(self) -> None:
        arguments = [{"name": "Runtime Probe"}, 7]

        class Oracle:
            identity = _identity("argument-array-oracle")

            def cases(self, component_lock):
                del component_lock
                return (
                    LocalIndependentAcceptanceCase.create(
                        "argument-array", arguments, {"arguments": arguments}
                    ),
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package-marker").write_bytes(b"immutable-package")
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=Oracle(),
            )
            custody = SimpleNamespace(
                root=root,
                tree_identity=local_tree_identity(root),
                native_sdk_resources=None,
            )
            plan = SimpleNamespace(
                identity=_identity("package-plan"),
                entrypoints=(SimpleNamespace(kind="portable-application"),),
            )
            result = SimpleNamespace(identity=_identity("package-result"))
            command = (
                sys.executable,
                "-c",
                "import json,sys; "
                "print(json.dumps({'arguments': json.loads(sys.argv[1])}))",
            )
            with (
                mock.patch.object(
                    ports, "project_package_custody", return_value=custody
                ),
                mock.patch.object(ports, "_packaged_argv", return_value=command),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
            ):
                evidence = ports.accept_project_independently(
                    mock.sentinel.component_lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )

        self.assertIsNotNone(evidence)

    def test_operational_index_sidecar_does_not_change_source_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("print('source')\n", encoding="utf-8")
            before = local_generated_source_tree_identity(root)
            sidecar = root / ".codegraph"
            sidecar.mkdir()
            (sidecar / "codegraph.db").write_bytes(b"operational-index")

            self.assertEqual(local_generated_source_tree_identity(root), before)

    def test_observed_toolchain_binding_retains_full_identity_command_and_guard(
        self,
    ) -> None:
        toolchain = discover_python_toolchain()

        binding = LocalComponentToolBinding.from_observed_toolchain(toolchain)

        self.assertEqual(binding.command, toolchain.command)
        self.assertEqual(binding.toolchain_identity.uri, toolchain.identity)
        binding.require_unchanged()

    def test_legacy_binding_identity_includes_launcher_arguments(self) -> None:
        plain = LocalComponentToolBinding(sys.executable)
        isolated = LocalComponentToolBinding(sys.executable, ("-I",))

        self.assertNotEqual(plain.toolchain_identity, isolated.toolchain_identity)
        self.assertEqual(isolated.command, (isolated.executable, "-I"))

    def test_directory_export_has_deterministic_retrievable_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            for directory in (first, second):
                (directory / "nested").mkdir(parents=True)
                (directory / "app.js").write_bytes(b"console.log('known')\n")
                (directory / "nested" / "data.txt").write_bytes(b"payload\n")

            first_bytes = _directory_export_bytes(first)
            second_bytes = _directory_export_bytes(second)

            self.assertEqual(first_bytes, second_bytes)
            with zipfile.ZipFile(BytesIO(first_bytes)) as archive:
                self.assertEqual(archive.namelist(), ["app.js", "nested/data.txt"])
                self.assertEqual(archive.read("nested/data.txt"), b"payload\n")

            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "directory artifact export is empty"
            ):
                _directory_export_bytes(root / "empty")

    def test_missing_generated_export_is_retryable_candidate_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "app"
            with self.assertRaises(GeneratedCandidateCommandError) as raised:
                _require_generated_export(missing)

        self.assertEqual(raised.exception.code, "builder.generated-source-rejected")
        self.assertEqual(
            str(raised.exception),
            "generated build did not produce the exact declared export",
        )

    def test_locked_commands_build_test_execute_and_hit_exact_cache(self) -> None:
        snapshot, execution = _fixture()
        generation_plan = execution.generation_plans[0]
        binding = LocalComponentToolBinding(sys.executable)
        build_script = (
            "from pathlib import Path; import shutil,sys; "
            "shutil.copy2(Path(sys.argv[1])/'app.py', Path(sys.argv[2]))"
        )
        commands = (
            ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                (
                    "{tool}",
                    "-c",
                    build_script,
                    "{source_root}",
                    "{export_path}",
                    "{object_root}",
                    "{provider_artifacts}",
                ),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.TEST,
                (
                    "{tool}",
                    "-c",
                    "from pathlib import Path; import json,sys; "
                    "assert 'known-output' in (Path(sys.argv[1])/'app').read_text(); "
                    "print(json.dumps(dict(schema="
                    "'literate-ai/generated-test-results@1',cases=["
                    "dict(case_id='fixture-example',outcome='passed'),"
                    "dict(case_id='fixture-boundary',outcome='passed'),"
                    "dict(case_id='fixture-invariant',outcome='passed')]),"
                    "sort_keys=True,separators=(',',':')))",
                    "{artifact_root}",
                ),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.EXECUTE,
                (
                    "{tool}",
                    "-c",
                    "from pathlib import Path; import os,sys; "
                    "print(Path(os.environ.get('LITAI_TEST_RUNTIME', "
                    "str(Path(sys.argv[1])/'app'))).read_text().strip())",
                    "{artifact_root}",
                    "--litai-smoke",
                ),
            ),
        )
        contract = ComponentCommandContract(
            component_revision=generation_plan.component_revision,
            locked_build_authority_identity=_identity("locked-build-authority"),
            build_system_resolver_identity=_identity("build-system-resolver"),
            build_system_toolchain_identity=_identity("build-system-toolchain"),
            language_compiler_identity=binding.toolchain_identity,
            language_runtime_identity=_identity("language-runtime"),
            commands=commands,
            tool_bindings=tuple(
                ComponentCommandToolBinding(phase, binding.toolchain_identity)
                for phase in ComponentCommandPhase
            ),
            artifact_export=ComponentArtifactExportShape(
                "app",
                "executable",
                _identity("abi"),
                _identity("target"),
                "application/vnd.literate-ai.executable",
                _identity("producer"),
            ),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_bytes(b"known-output\n")
            registry = LocalSourceTreeRegistry()
            candidate = register_strict_source(
                registry,
                source,
                snapshot=snapshot,
                generation_plan=generation_plan,
                identity_namespace="standard-local-command-test",
            )
            ports = LocalStandardLifecyclePorts(
                source_trees=registry,
                object_root=root / "objects",
                contracts=(contract,),
                tool_bindings=(binding,),
            )
            recorder = QualificationEvidenceRecorder(
                max_bytes=1_000_000, max_records=400
            )
            ports.retain_evidence_with(recorder)
            # This test exercises one exact plan node; validation of complete project
            # coverage is separately enforced by the application service.
            intent = ports.create(execution, generation_plan, candidate, (), ())
            index = ports.index(candidate.component_revision, candidate.tree_identity)
            authorization = ports.authorize(intent, index)
            plan = ports.finalize(intent, authorization)
            self.assertEqual(
                plan.manifest.build_system_driver_identity,
                contract.build_system_toolchain_identity,
            )
            self.assertEqual(
                plan.request.build_system_resolver_identity,
                contract.build_system_resolver_identity,
            )
            self.assertEqual(
                plan.request.build_system_toolchain_identity,
                contract.build_system_toolchain_identity,
            )
            self.assertEqual(
                plan.request.language_compiler_identity,
                contract.language_compiler_identity,
            )
            self.assertEqual(
                plan.request.language_runtime_identity,
                contract.language_runtime_identity,
            )
            with (
                mock.patch.object(
                    ports,
                    "_run",
                    return_value=subprocess.CompletedProcess((), 0, "", ""),
                ),
                self.assertRaises(GeneratedCandidateCommandError) as rejected,
            ):
                ports.build(plan, ())
            self.assertEqual(
                rejected.exception.code, "builder.generated-source-rejected"
            )
            first = ports.build(plan, ())
            test_identity = ports.test(plan, first.exports)
            execution_identity = ports.execute(plan, first.exports)
            self.assert_late_provider_execution(
                ports, execution, plan, first.exports, recorder
            )
            self.assert_scoped_execution(
                ports, execution, plan, first.exports, recorder
            )
            rerun = ports.execution_command(plan, first.exports)
            acceptance = ports.accept(
                plan, test_identity.identity, execution_identity.identity
            )
            second = ports.build(plan, ())
            fresh_intent = ports.create(execution, generation_plan, candidate, (), ())
            fresh_index = ports.index(
                candidate.component_revision, candidate.tree_identity
            )
            fresh_authorization = ports.authorize(fresh_intent, fresh_index)
            fresh_plan = ports.finalize(fresh_intent, fresh_authorization)
            rebound = ports.build(fresh_plan, ())

            self.assertEqual(first, second)
            self.assertNotEqual(first, rebound)
            self.assertEqual(
                rebound.exports[0].authorization_identity,
                fresh_authorization.authorization_identity,
            )
            self.assertEqual(
                ports.read_artifact_blob(rebound.exports[0].blob), b"known-output\n"
            )
            self.assertIsInstance(first.evidence, StandardBuildEvidence)
            self.assertIsInstance(test_identity, StandardGeneratedTestExecutionEvidence)
            self.assertIsInstance(execution_identity, StandardExecutionEvidence)
            self.assertEqual(acceptance.build.identity, first.evidence.identity)
            self.assertEqual(len(test_identity.cases), 3)
            self.assertEqual(
                ports.read_artifact_blob(first.exports[0].blob), b"known-output\n"
            )
            self.assertIn(
                b'"bomFormat":"CycloneDX"',
                ports.resolved_sbom_content(first.evidence),
            )
            self.assertIn(
                b'"bomFormat":"CycloneDX"',
                ports.source_sbom_content(first.evidence),
            )
            self.assertEqual(ports.build_cache_misses, 1)
            self.assertEqual(ports.build_cache_hits, 2)
            self.assertEqual(
                ports.execution_stdout[candidate.component_revision.uri],
                "known-output",
            )
            self.assertEqual(rerun.argv[0], binding.executable)
            self.assertNotIn("--litai-smoke", rerun.argv)
            self.assertEqual(rerun.cwd, ports.artifact_path(first.exports[0]).parent)
            self.assertEqual(rerun.environment, ())
            repeated_execution = subprocess.run(
                rerun.argv,
                cwd=rerun.cwd,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(repeated_execution.stdout.strip(), "known-output")
            self.assertNotEqual(test_identity.identity, execution_identity.identity)
            self.assertEqual(
                local_generated_source_tree_identity(source), candidate.tree_identity
            )

            altered_authorizations = []
            for changes in (
                {"classification_digest": _identity("foreign-index").uri},
                {"effective_revision_digest": _identity("foreign-component").uri},
                {"privileges": ("execute-build-tools", "network-access")},
                {"privileges": ()},
                {"revoked": True},
                {
                    "issued_at": datetime(2020, 1, 1, tzinfo=UTC),
                    "expires_at": datetime(2020, 1, 2, tzinfo=UTC),
                },
            ):
                changed = replace(
                    authorization, grant=replace(authorization.grant, **changes)
                )
                recorder.remember_json(changed.grant.to_dict())
                recorder.remember_json(changed.to_dict())
                altered_authorizations.append(
                    (changes, changed, ports.finalize(intent, changed))
                )

        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        for changes, changed, changed_plan in altered_authorizations:
            arguments = dict(
                candidate=candidate,
                index_identity=index,
                authorization_identity=changed.authorization_identity,
                plan=changed_plan,
            )
            with self.subTest(grant_fields=tuple(changes)):
                if "expires_at" in changes:
                    # Historical record integrity is independent of today's clock.
                    verify_qualification_build_authorization(reader, **arguments)
                else:
                    with self.assertRaisesRegex(
                        QualificationCaptureError, "build-authorization-mismatch"
                    ):
                        verify_qualification_build_authorization(reader, **arguments)
        verify_qualification_build_authorization(
            reader,
            candidate=candidate,
            index_identity=index,
            authorization_identity=authorization.authorization_identity,
            plan=plan,
        )
        verify_qualification_command_authority(
            reader,
            contract=contract,
            authorization_identity=authorization.authorization_identity,
            plan=plan,
        )
        self.assert_reopened_build(recorder, plan, first.evidence)
        self.assert_reopened_generated_tests(
            recorder,
            plan,
            first.evidence,
            test_identity,
            LockedComponentNodePreparationAdapter()
            .project(snapshot, generation_plan)
            .recipe,
            snapshot.authority.lock,
        )
        self.assert_reopened_execution(recorder, plan, execution_identity)

        # Reopen the stage records and observations after all source/artifact paths
        # have been removed, rather than relying on the runtime's object maps.
        self.assertFalse(root.exists())
        records = dict(recorder.entries)
        for evidence in (
            intent,
            authorization,
            test_identity,
            execution_identity,
            acceptance,
        ):
            self.assertEqual(
                type(evidence).from_dict(json.loads(records[evidence.identity])),
                evidence,
            )
        self.assertEqual(
            canonical_identity(
                json.loads(records[authorization.authorization_identity])
            ),
            first.exports[0].authorization_identity,
        )
        indexed = json.loads(records[index])
        self.assertEqual(indexed["tree"], candidate.tree_identity.uri)
        self.assertEqual(indexed["component"], candidate.component_revision.uri)
        self.assertEqual(
            canonical_identity(json.loads(records[intent.build_request_identity])),
            authorization.build_request_identity,
        )
        for case in test_identity.cases:
            case_document = json.loads(records[case.case_identity])
            observation = json.loads(records[case.observation_identity])
            self.assertEqual(case_document["case_id"], case.case_id)
            self.assertEqual(observation["case_identity"], case.case_identity.uri)
            process_uri = observation["suite_process_observation_identity"]
            self.assertIn(process_uri, {identity.uri for identity in records})
        self.assertEqual(
            json.loads(records[execution_identity.execution_contract_identity]),
            contract.command(ComponentCommandPhase.EXECUTE).to_dict(),
        )
        for identity in (
            test_identity.test_custody_identity,
            execution_identity.observation_identity,
            execution_identity.stdout_identity,
            execution_identity.stderr_identity,
            acceptance.acceptance_policy_identity,
        ):
            self.assertEqual(
                canonical_identity(json.loads(records[identity])), identity
            )

    def test_retained_build_evidence_reopens_after_workspace_cleanup(self) -> None:
        recorder = QualificationEvidenceRecorder(max_bytes=1_000_000, max_records=100)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, candidate, _intent = _python_copy_lifecycle(root)
            ports.retain_evidence_with(recorder)
            built = ports.build(plan, ())
            custody = ports.source_trees.evidence(candidate.tree_identity)
            expected_source = custody.source_bom_content
            self.assertEqual(ports.source_sbom_content(built.evidence), expected_source)
            expected_resolved = ports.resolved_sbom_content(built.evidence)
            expected_suite = custody.generated_test_suite_content
            expected_suite_uri = custody.generated_test_suite.content_identity
            expected_custody = custody.identity

        self.assertFalse(root.exists())
        records = dict(recorder.entries)
        reopened = StandardBuildEvidence.from_dict(
            json.loads(records[built.evidence.identity])
        )
        self.assertEqual(reopened, built.evidence)
        self.assertEqual(type(plan).from_dict(json.loads(records[plan.identity])), plan)
        self.assertEqual(records[reopened.source_sbom.bom_identity], expected_source)
        self.assertEqual(
            records[reopened.resolved_sbom.bom_identity], expected_resolved
        )
        self.assertEqual(
            {identity.uri: payload for identity, payload in records.items()}[
                expected_suite_uri
            ],
            expected_suite,
        )
        custody_document = json.loads(records[expected_custody])
        self.assertEqual(
            canonical_identity(custody_document), reopened.source_custody_identity
        )
        self.assertEqual(custody_document["candidate_identity"], candidate.identity.uri)
        self.assertEqual(json.loads(records[candidate.identity]), candidate.to_dict())
        for identity, payload in records.items():
            self.assertEqual(
                identity.uri, "sha256:" + hashlib.sha256(payload).hexdigest()
            )

    def test_cache_rejects_coordinated_self_manifest_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, _candidate, _intent = _python_copy_lifecycle(Path(temporary))
            first = ports.build(plan, ())
            rewrite_self_authenticating_artifact(
                ports.artifact_path(first.exports[0]).parent,
                "app",
                b"tampered-output\n",
            )

            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "changed after publication"
            ):
                ports.build(plan, ())

    def test_restarted_runtime_hits_durable_external_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, plan, _candidate, _intent = _python_copy_lifecycle(Path(temporary))
            expected = first.build(plan, ())
            second = LocalStandardLifecyclePorts(
                source_trees=first.source_trees,
                object_root=first.object_root,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
            )
            with mock.patch.object(second, "_run_locked") as run:
                cached = second.build(plan, ())

            self.assertEqual(cached, expected)
            run.assert_not_called()
            self.assertEqual(second.build_cache_misses, 0)
            self.assertEqual(second.build_cache_hits, 1)

    def test_restarted_runtime_rejects_rewritten_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, plan, _candidate, _intent = _python_copy_lifecycle(Path(temporary))
            built = first.build(plan, ())
            rewrite_self_authenticating_artifact(
                first.artifact_path(built.exports[0]).parent,
                "app",
                b"tampered-output\n",
            )
            second = LocalStandardLifecyclePorts(
                source_trees=first.source_trees,
                object_root=first.object_root,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
            )

            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "changed after publication"
            ):
                second.build(plan, ())

    def test_artifact_bytes_without_checkpoint_are_rebuilt_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, plan, _candidate, _intent = _python_copy_lifecycle(root)
            expected = first.build(plan, ())
            isolated = root / "isolated-objects"
            copy_digest_cache_without_sidecars(first.object_root, isolated)
            second = LocalStandardLifecyclePorts(
                source_trees=first.source_trees,
                object_root=isolated,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
            )
            replayed = second.build(plan, ())
            cached = second.build(plan, ())

            self.assertEqual(replayed, expected)
            self.assertEqual(cached, expected)
            self.assertEqual(second.build_cache_misses, 1)
            self.assertEqual(second.build_cache_hits, 1)

    def test_later_authorization_reuses_command_addressed_cache_slot(self) -> None:
        """A later grant must reuse the sealed slot, not fight it.

        Local authorization embeds wall-clock issued_at, so `litai test` after
        `litai build` finalizes a different plan identity while keeping the same
        command-addressed cache key. The external checkpoint authenticates the
        sealed tree for that key; grant equality is audit metadata.
        """

        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, candidate, intent = _python_copy_lifecycle(Path(temporary))
            first = ports.build(plan, ())
            ports.clock = lambda: datetime(2099, 1, 1, tzinfo=UTC)
            later_plan = ports.finalize(
                intent,
                ports.authorize(
                    intent,
                    ports.index(candidate.component_revision, candidate.tree_identity),
                ),
            )
            self.assertNotEqual(plan.identity, later_plan.identity)
            with mock.patch.object(ports, "_run_locked") as run:
                cached = ports.build(later_plan, ())

            self.assertEqual(
                ports.read_artifact_blob(cached.exports[0].blob),
                ports.read_artifact_blob(first.exports[0].blob),
            )
            run.assert_not_called()
            self.assertEqual(ports.build_cache_misses, 1)
            self.assertEqual(ports.build_cache_hits, 1)

    def test_multi_entrypoint_build_test_execute_and_cache_cover_every_export(
        self,
    ) -> None:
        snapshot, execution = _fixture()
        generation_plan = execution.generation_plans[0]
        binding = LocalComponentToolBinding(sys.executable)
        build = ComponentLifecycleCommand(
            ComponentCommandPhase.BUILD,
            (
                "{tool}",
                "-c",
                "from pathlib import Path; import shutil,sys; "
                "source=Path(sys.argv[1]); root=Path(sys.argv[2]); "
                "shutil.copy2(source/'app.py',Path(sys.argv[3])); "
                "shutil.copy2(source/'worker.py',root/'worker')",
                "{source_root}",
                "{artifact_root}",
                "{export_path}",
                "{object_root}",
            ),
        )
        result_script = (
            "from pathlib import Path; import json,sys; "
            "assert Path(sys.argv[1]).read_text().strip() in "
            "('primary-output','worker-output'); "
            "print(json.dumps(dict(schema='literate-ai/generated-test-results@1',"
            "cases=[dict(case_id='fixture-example',outcome='passed'),"
            "dict(case_id='fixture-boundary',outcome='passed'),"
            "dict(case_id='fixture-invariant',outcome='passed')]),"
            "sort_keys=True,separators=(',',':')))"
        )
        execute_script = (
            "from pathlib import Path; import json,sys; "
            "print(json.dumps(Path(sys.argv[1]).read_text().strip()))"
        )
        primary_test = ComponentLifecycleCommand(
            ComponentCommandPhase.TEST,
            ("{tool}", "-c", result_script, "{export_path}", "{artifact_root}"),
        )
        primary_execute = ComponentLifecycleCommand(
            ComponentCommandPhase.EXECUTE,
            (
                "{tool}",
                "-c",
                execute_script,
                "{export_path}",
                "{artifact_root}",
                "--litai-smoke",
            ),
        )
        primary_shape = ComponentArtifactExportShape(
            "app",
            "portable-application",
            _identity("primary-abi"),
            _identity("target"),
            "application/vnd.literate-ai.executable",
            _identity("producer"),
        )
        worker_shape = ComponentArtifactExportShape(
            "worker",
            "portable-application",
            _identity("worker-abi"),
            _identity("target"),
            "application/vnd.literate-ai.executable",
            _identity("producer"),
        )
        runtime_bindings = (
            ComponentCommandToolBinding(
                ComponentCommandPhase.TEST, binding.toolchain_identity
            ),
            ComponentCommandToolBinding(
                ComponentCommandPhase.EXECUTE, binding.toolchain_identity
            ),
        )
        contract = ComponentCommandContract(
            component_revision=generation_plan.component_revision,
            locked_build_authority_identity=_identity("multi-build-authority"),
            build_system_resolver_identity=_identity("multi-build-resolver"),
            build_system_toolchain_identity=binding.toolchain_identity,
            language_compiler_identity=binding.toolchain_identity,
            language_runtime_identity=binding.toolchain_identity,
            commands=(build, primary_test, primary_execute),
            tool_bindings=tuple(
                ComponentCommandToolBinding(phase, binding.toolchain_identity)
                for phase in ComponentCommandPhase
            ),
            artifact_export=primary_shape,
            entrypoint_contracts=(
                ComponentEntrypointCommandContract(
                    primary_shape.identity,
                    "primary",
                    (primary_test, primary_execute),
                    runtime_bindings,
                    primary_shape,
                ),
                ComponentEntrypointCommandContract(
                    worker_shape.identity,
                    "worker",
                    (
                        ComponentLifecycleCommand(
                            ComponentCommandPhase.TEST,
                            (
                                "{tool}",
                                "-c",
                                result_script,
                                "{export_path}",
                                "{artifact_root}",
                            ),
                        ),
                        ComponentLifecycleCommand(
                            ComponentCommandPhase.EXECUTE,
                            (
                                "{tool}",
                                "-c",
                                execute_script,
                                "{export_path}",
                                "{artifact_root}",
                                "--litai-smoke",
                            ),
                        ),
                    ),
                    runtime_bindings,
                    worker_shape,
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_text("primary-output\n", encoding="utf-8")
            (source / "worker.py").write_text("worker-output\n", encoding="utf-8")
            registry = LocalSourceTreeRegistry()
            candidate = register_strict_source(
                registry,
                source,
                snapshot=snapshot,
                generation_plan=generation_plan,
                identity_namespace="standard-local-multi-entrypoint-test",
            )

            class ExactOutputOracle:
                def __init__(self, unit: str, expected: str) -> None:
                    self.identity = _identity(f"oracle-{unit}")
                    self._case = LocalIndependentAcceptanceCase.create(
                        f"accept-{unit}", [], expected
                    )

                def cases(self, _component_lock):
                    return (self._case,)

            oracle_bundle = ComponentAcceptanceOracleBundle(
                (
                    ComponentAcceptanceOracleBinding(
                        "app",
                        "primary",
                        "portable-application",
                        ExactOutputOracle("primary", "primary-output"),
                    ),
                    ComponentAcceptanceOracleBinding(
                        "worker",
                        "worker",
                        "portable-application",
                        ExactOutputOracle("worker", "worker-output"),
                    ),
                )
            )
            ports = LocalStandardLifecyclePorts(
                source_trees=registry,
                object_root=root / "objects",
                contracts=(contract,),
                tool_bindings=(binding,),
                independent_acceptance_oracle=oracle_bundle,
            )
            recorder = QualificationEvidenceRecorder(
                max_bytes=1_000_000, max_records=300
            )
            ports.retain_evidence_with(recorder)
            intent = ports.create(execution, generation_plan, candidate, (), ())
            authorization = ports.authorize(
                intent,
                ports.index(candidate.component_revision, candidate.tree_identity),
            )
            plan = ports.finalize(intent, authorization)
            first = ports.build(plan, ())
            primary_rerun = ports.execution_command(
                plan,
                first.exports,
                entrypoint_identity=contract.entrypoint_contracts[
                    0
                ].entrypoint_identity,
            )
            worker_rerun = ports.execution_command(
                plan,
                first.exports,
                entrypoint_identity=contract.entrypoint_contracts[
                    1
                ].entrypoint_identity,
            )
            tests = ports.test(plan, first.exports)
            from literate_ai.adapters.standard_test_admission import (
                verify_transferred_tests,
            )

            test_reader = QualificationEvidenceReader(
                recorder.entries, max_bytes=1_000_000, max_records=300
            )
            test_arguments = dict(
                plan=plan,
                build=first.evidence,
                source_custody=registry.evidence(candidate.tree_identity),
                contract=contract,
                evidence=tests,
            )
            verify_transferred_tests(test_reader, **test_arguments)
            altered_units = (
                *contract.entrypoint_contracts[:-1],
                replace(
                    contract.entrypoint_contracts[-1], deployment_unit="another-unit"
                ),
            )
            with self.assertRaisesRegex(ValueError, "entrypoint authority"):
                verify_transferred_tests(
                    test_reader,
                    **(
                        test_arguments
                        | {
                            "contract": replace(
                                contract, entrypoint_contracts=altered_units
                            )
                        }
                    ),
                )
            from tests.unit.multi_execute_worker_fixture import (
                assert_multi_execute_worker,
            )

            worker_stdout = assert_multi_execute_worker(
                self, ports, execution, plan, first
            )
            executed = ports.execute(plan, first.exports)
            self.assertEqual(
                worker_stdout, ports.execution_stdout[plan.component_revision.uri]
            )
            self.assert_scoped_execution(
                ports, execution, plan, first.exports, recorder
            )
            accepted = ports.accept(plan, tests.identity, executed.identity)
            manifest = realize_manifest(plan.manifest, first.exports)
            graph = create_artifact_build_graph(
                build_system_driver_identity=manifest.build_system_driver_identity,
                manifests=(manifest,),
                link_roots=(),
                link_root_groups=(tuple(item.identity for item in first.exports),),
            )
            package_lock = SimpleNamespace(
                root_revision=generation_plan.component_revision,
                identity=execution.component_lock_identity,
                nodes=(
                    SimpleNamespace(
                        revision=SimpleNamespace(
                            identity=generation_plan.component_revision,
                            definition=SimpleNamespace(
                                entrypoints=(
                                    SimpleNamespace(
                                        name="app",
                                        kind="portable-application",
                                        resolved_deployment_unit="primary",
                                    ),
                                    SimpleNamespace(
                                        name="worker",
                                        kind="portable-application",
                                        resolved_deployment_unit="worker",
                                    ),
                                )
                            ),
                        )
                    ),
                ),
            )
            package_plan, package_result = ports.create_project_package(
                package_lock,
                execution,
                SimpleNamespace(),
                graph,
                graph.link_plans[0],
            )
            packaged_tests = ports.test_root_integration(
                package_lock,
                execution,
                SimpleNamespace(),
                package_plan,
                package_result,
            )
            packaged_execution = ports.execute_packaged_project(
                package_lock,
                execution,
                SimpleNamespace(),
                package_plan,
                package_result,
            )
            independent = ports.accept_project_independently(
                package_lock,
                execution,
                SimpleNamespace(),
                package_plan,
                package_result,
                packaged_tests,
                packaged_execution,
            )
            ports._execution_evidence.clear()
            first_process = subprocess.CompletedProcess(
                ("fixture",), 0, '"primary-output"\n', ""
            )
            with (
                mock.patch.object(
                    ports,
                    "_run_locked",
                    side_effect=(
                        first_process,
                        LocalStandardLifecycleError("second unit failed"),
                    ),
                ),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError, "second unit failed"
                ),
            ):
                ports.execute(plan, first.exports)
            second = ports.build(plan, ())

        self.assertEqual(intent.build_request.allowed_outputs, ("app", "worker"))
        self.assertEqual(
            plan.manifest.actions[0].declared_output_ids, ("app", "worker")
        )
        self.assertEqual(
            tuple(item.export_id for item in first.exports), ("app", "worker")
        )
        self.assertNotEqual(primary_rerun.argv, worker_rerun.argv)
        self.assertIn(str(primary_rerun.cwd / "app"), primary_rerun.argv)
        self.assertIn(str(worker_rerun.cwd / "worker"), worker_rerun.argv)
        self.assertEqual(primary_rerun.cwd, worker_rerun.cwd)
        self.assertEqual(first, second)
        self.assertEqual(len(tests.entrypoint_evidence or ()), 2)
        self.assertEqual(len(executed.entrypoint_evidence or ()), 2)
        self.assertEqual(
            tuple(item.deployment_unit for item in package_plan.entrypoints),
            ("primary", "worker"),
        )
        self.assertIsNotNone(packaged_tests)
        self.assertIsNotNone(packaged_execution)
        self.assertIsNotNone(independent)
        self.assertFalse(ports._execution_evidence)
        self.assertEqual(
            accepted.build.export_identities,
            first.evidence.export_identities,
        )
        self.assertEqual(ports.build_cache_misses, 1)
        self.assertEqual(ports.build_cache_hits, 1)

        self.assertFalse(root.exists())
        self.assert_reopened_execution(recorder, plan, executed)
        self.assert_reopened_generated_tests(
            recorder,
            plan,
            first.evidence,
            tests,
            LockedComponentNodePreparationAdapter()
            .project(snapshot, generation_plan)
            .recipe,
            snapshot.authority.lock,
        )
        records = dict(recorder.entries)
        for identity in (packaged_tests, packaged_execution, independent):
            document = json.loads(records[identity])
            self.assertEqual(canonical_identity(document), identity)
            for observation in document["entrypoints"]:
                self.assertIn(
                    observation["observation_identity"],
                    {item.uri for item in records},
                )
        for evidence in (tests, executed, accepted):
            self.assertEqual(
                type(evidence).from_dict(json.loads(records[evidence.identity])),
                evidence,
            )
        for unit in tests.entrypoint_evidence + executed.entrypoint_evidence:
            self.assertEqual(
                type(unit).from_dict(json.loads(records[unit.identity])), unit
            )
        for unit in tests.entrypoint_evidence:
            for case in unit.cases:
                self.assertIn(case.case_identity, records)
                self.assertIn(case.observation_identity, records)
            self.assertIn(unit.test_custody_identity, records)
            self.assertIn(unit.process_observation_identity, records)
        for unit in executed.entrypoint_evidence:
            for identity in (
                unit.observation_identity,
                unit.stdout_identity,
                unit.stderr_identity,
            ):
                self.assertIn(identity, records)

    def test_phase_launchers_require_and_use_their_exact_tool_bindings(self) -> None:
        _snapshot, execution = _fixture()
        generation_plan = execution.generation_plans[0]
        commands = (
            ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                ("{tool}", "{source_root}", "{object_root}", "{export_path}"),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.TEST, ("{tool}", "{artifact_root}")
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.EXECUTE, ("{tool}", "{artifact_root}")
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bindings = []
            for phase in ComponentCommandPhase:
                launcher = root / f"{phase.value}-tool"
                launcher.write_bytes(f"exact-{phase.value}".encode())
                bindings.append(LocalComponentToolBinding(str(launcher)))
            contract = ComponentCommandContract(
                component_revision=generation_plan.component_revision,
                locked_build_authority_identity=_identity("phase-authority"),
                build_system_resolver_identity=_identity("phase-resolver"),
                build_system_toolchain_identity=bindings[0].toolchain_identity,
                language_compiler_identity=bindings[0].toolchain_identity,
                language_runtime_identity=bindings[2].toolchain_identity,
                commands=commands,
                tool_bindings=tuple(
                    ComponentCommandToolBinding(phase, binding.toolchain_identity)
                    for phase, binding in zip(
                        ComponentCommandPhase, bindings, strict=True
                    )
                ),
                artifact_export=ComponentArtifactExportShape(
                    "app",
                    "executable",
                    _identity("phase-abi"),
                    _identity("phase-target"),
                    "application/octet-stream",
                    _identity("phase-producer"),
                ),
            )
            with self.assertRaisesRegex(ValueError, "every and only locked"):
                LocalStandardLifecyclePorts(
                    source_trees=LocalSourceTreeRegistry(),
                    object_root=root / "missing-binding-objects",
                    contracts=(contract,),
                    tool_bindings=tuple(bindings[:2]),
                )
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(contract,),
                tool_bindings=tuple(bindings),
            )
            source = root / "source"
            objects = root / "phase-objects"
            artifact = root / "artifact"
            for path in (source, objects, artifact):
                path.mkdir()
            completed = subprocess.CompletedProcess((), 0, "", "")
            with mock.patch.object(ports, "_run", return_value=completed) as run:
                for phase, binding in zip(ComponentCommandPhase, bindings, strict=True):
                    ports._run_locked(
                        contract,
                        phase,
                        source_root=source,
                        object_root=objects,
                        artifact_root=artifact,
                        export_path=artifact / "app",
                        providers=(),
                    )
                    self.assertEqual(run.call_args.args[0][0], binding.executable)

            for phase, expected_code in (
                (
                    ComponentCommandPhase.BUILD,
                    "builder.generated-source-rejected",
                ),
                (ComponentCommandPhase.TEST, "generated-test.failed"),
            ):
                with (
                    mock.patch.object(
                        ports,
                        "_run",
                        side_effect=LocalStandardLifecycleError(
                            "generated command exited with status 1"
                        ),
                    ),
                    self.assertRaises(GeneratedCandidateCommandError) as raised,
                ):
                    ports._run_locked(
                        contract,
                        phase,
                        source_root=source,
                        object_root=objects,
                        artifact_root=artifact,
                        export_path=artifact / "app",
                        providers=(),
                    )
                self.assertEqual(raised.exception.code, expected_code)

            with (
                mock.patch.object(
                    ports,
                    "_run",
                    side_effect=LocalStandardLifecycleError(
                        "generated executable exited with status 1"
                    ),
                ),
                self.assertRaises(LocalStandardLifecycleError) as raised,
            ):
                ports._run_locked(
                    contract,
                    ComponentCommandPhase.EXECUTE,
                    source_root=source,
                    object_root=objects,
                    artifact_root=artifact,
                    export_path=artifact / "app",
                    providers=(),
                )
            self.assertNotIsInstance(raised.exception, GeneratedCandidateCommandError)

    def test_tool_binding_detects_executable_byte_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "python-copy"
            shutil.copy2(sys.executable, launcher)
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
            binding = LocalComponentToolBinding(str(launcher))
            with launcher.open("ab") as stream:
                stream.write(b"drift")
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "changed after binding"
            ):
                binding.require_unchanged()

    def test_generated_test_protocol_rejection_is_attributable_to_candidate(self):
        snapshot, execution = _fixture()
        revision = execution.generation_plans[0].component_revision
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
                tool_bindings=(),
            )
            plan = SimpleNamespace(component_revision=revision)
            rejected = LocalStandardLifecycleError(
                "generated-test runner did not emit attributable case results"
            )
            with (
                mock.patch.object(ports, "_test_locked", side_effect=rejected),
                self.assertRaises(GeneratedCandidateCommandError) as raised,
            ):
                ports.test(plan, ())
            self.assertEqual(raised.exception.code, "generated-test.failed")
            self.assertIn(
                "GeneratedCandidateCommandError",
                ports.failure_diagnostics[revision.uri],
            )

    def test_locked_toolchain_must_match_measured_binding(self) -> None:
        _snapshot, execution = _fixture()
        plan = execution.generation_plans[0]
        binding = LocalComponentToolBinding(sys.executable)
        commands = (
            ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                ("{tool}", "x", "{source_root}", "{object_root}", "{export_path}"),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.TEST,
                ("{tool}", "x", "{artifact_root}"),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.EXECUTE,
                ("{tool}", "x", "{artifact_root}"),
            ),
        )
        contract = ComponentCommandContract(
            component_revision=plan.component_revision,
            locked_build_authority_identity=_identity("authority"),
            build_system_resolver_identity=_identity("resolver"),
            build_system_toolchain_identity=_identity("different-toolchain"),
            language_compiler_identity=_identity("different-toolchain"),
            language_runtime_identity=_identity("different-toolchain"),
            commands=commands,
            tool_bindings=tuple(
                ComponentCommandToolBinding(phase, _identity("different-toolchain"))
                for phase in ComponentCommandPhase
            ),
            artifact_export=ComponentArtifactExportShape(
                "app",
                "executable",
                _identity("abi"),
                _identity("target"),
                "application/octet-stream",
                _identity("producer"),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "every and only locked"):
                LocalStandardLifecyclePorts(
                    source_trees=LocalSourceTreeRegistry(),
                    object_root=Path(temporary),
                    contracts=(contract,),
                    tool_bindings=(binding,),
                )


if __name__ == "__main__":
    unittest.main()
