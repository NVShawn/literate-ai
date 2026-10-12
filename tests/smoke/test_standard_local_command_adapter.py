"""Provider-neutral command authority realized by the local Standard adapter."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.component_acceptance import (
    SERVICE_SCHEMA,
    load_persistent_service_acceptance,
)
from literate_ai.adapters.generation_preparation import (
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    local_generated_source_tree_identity,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import (
    GeneratedCandidateCommandError,
    _directory_export_bytes,
    _local_tree_identity,
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
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_inputs,
)
from literate_ai.contracts import (
    ArtifactAssemblyDependency,
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
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
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.standard_source_evidence_fixture import register_strict_source


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

            from literate_ai.adapters.packaging import DirectoryPackageAdapter

            def dispatch_package(remote_graph, remote_plan, *, read_blob):
                self.assertEqual(remote_graph, graph)
                self.assertEqual(remote_plan, package_plan)
                return DirectoryPackageAdapter().package(
                    remote_plan, read_blob=read_blob
                )

            ports.project_packager = mock.Mock()
            ports.project_packager.package_local_inputs.side_effect = dispatch_package
            remote_plan, remote_result = ports.create_project_package(
                snapshot.authority.lock,
                execution,
                SimpleNamespace(),
                graph,
                graph.link_plans[0],
            )
            ports.project_packager.package_local_inputs.assert_called_once()
            self.assertEqual(
                (remote_plan, remote_result), (package_plan, package_result)
            )
            self.assertEqual(
                ports.project_package_custody(
                    remote_plan, remote_result
                ).package_result,
                package_result,
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


if __name__ == "__main__":
    unittest.main()
