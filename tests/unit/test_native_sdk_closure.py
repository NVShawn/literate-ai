"""SDK-backed linked providers execute from a relocated Standard package.

Component locking, native production and package custody are real. Consumer
source and build observations remain explicit fixtures, as in the SDK authority
suite; this does not qualify public source generation.
"""

import json
import shutil
import tempfile
import unittest
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.lifecycle import (
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
)
from literate_ai.adapters.native_sdk_package_scope import NativeSdkPackageExecutionScope
from literate_ai.adapters.native_sdk_qualification import (
    packaged_sdk_process_fields,
    sdk_process_fields,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.application.artifact_graph import create_artifact_build_graph
from literate_ai.application.standard_project_lifecycle import StandardProjectBuildPlan
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.executable_components import (
    ComponentCommandPhase,
    ComponentLifecycleCommand,
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.security import AuthorizationError, BuildAuthorization
from tests.unit import test_native_sdk_standard_authority
from tests.unit.test_standard_local_command_adapter import (
    _provider_export,
    _python_copy_lifecycle,
)


def linked_recipe(fixture):
    root = fixture.component
    original = root.with_name("original")
    root.rename(original)
    root.mkdir()
    original.rename(root / "provider")
    provider = root / "provider"
    for name in ("specs", "acceptance"):
        shutil.copytree(provider / name, root / name)
    path = provider / "component.md"
    document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
    application = deepcopy(document)
    application["source_dependencies"] = []
    application["provides"][0]["name"] = "sample.wrapper-app"
    application["requires"] = [
        dict(
            requirement_id="provider",
            capability="sample.portable-app",
            version_range=">=1,<2",
            dependency_kind="runtime",
            optional=False,
            constraints=[],
        )
    ]
    (root / "component.md").write_bytes(render_authoring_markdown(application, body))
    document["entrypoints"] = []
    document["provides"][0]["interface"] = {"uri": "integration.md", "pin": None}
    path.write_bytes(render_authoring_markdown(document, body))


def runtime_contract(contract):
    return replace(
        contract,
        commands=tuple(
            command
            if command.phase is ComponentCommandPhase.BUILD
            else ComponentLifecycleCommand(
                command.phase,
                (
                    "{tool}",
                    "-I",
                    "-B",
                    "-c",
                    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
                    "{native_sdk_inputs}",
                    "{artifact_root}",
                    "{export_path}",
                    "file",
                    "app",
                    "{provider_artifacts}",
                    "--litai-test"
                    if command.phase is ComponentCommandPhase.TEST
                    else "--litai-smoke",
                ),
            )
            for command in contract.commands
        ),
    )


class NativeSdkClosureTests(unittest.TestCase):
    def test_packaged_provider_roles_use_custody_without_rewriting_command_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _, _ = _python_copy_lifecycle(root)
            original = ports.contracts[plan.component_revision.uri]
            provider = _provider_export("library")
            root_export = _provider_export("application")
            deleted = root / "deleted producer"
            literal = "print(" + repr(str(deleted)) + ")"
            ports.contracts[plan.component_revision.uri] = replace(
                original,
                commands=tuple(
                    ComponentLifecycleCommand(
                        command.phase,
                        (
                            "{tool}",
                            "-c",
                            literal,
                            "{artifact_root}",
                            "{provider_artifacts}",
                        ),
                    )
                    if command.phase is ComponentCommandPhase.EXECUTE
                    else command
                    for command in original.commands
                ),
            )
            package = root / "relocated package"
            library = package / "provider" / "library"
            library.parent.mkdir(parents=True)
            library.write_text("packaged provider")
            app = package / "app"
            app.write_text("packaged application")
            ports._planned_exports[plan.component_revision.uri] = root_export
            ports._artifact_paths[provider.identity.uri] = deleted
            custody = SimpleNamespace(
                root_plan=plan,
                root=package,
                artifact_paths={
                    provider.identity.uri: library,
                    root_export.identity.uri: app,
                },
                python_dependency_observer=None,
                python_artifact_tree_identity=None,
            )
            with patch.object(
                ports, "_intent_artifacts_for_plan", return_value=(provider,)
            ):
                argv = ports._packaged_argv(custody, ComponentCommandPhase.EXECUTE)
            self.assertEqual(argv[2], literal)
            self.assertEqual(argv[-1], str(library.parent.resolve()))
            self.assertFalse(deleted.exists())

    def test_linked_provider_sdk_executes_and_retains_owner_authority(self):
        fixture = test_native_sdk_standard_authority.NativeSdkStandardAuthorityTests()
        self.addCleanup(fixture.doCleanups)
        fixture.configure_recipe_fixture = linked_recipe
        fixture.setUp()
        lock = fixture.fixture.snapshot.authority.lock
        self.assertNotEqual(lock.root_revision, fixture.contract.component_revision)
        root_contract = runtime_contract(
            replace(fixture.contract, component_revision=lock.root_revision)
        )
        interface_bytes = (
            fixture.fixture.recipe_fixture.component / "provider/integration.md"
        ).read_bytes()
        import hashlib

        interface = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(interface_bytes).hexdigest()
        )
        provider_contract = replace(
            fixture.contract,
            library_import_surface=LibraryImportSurface(
                "python",
                "consumer",
                (
                    LibraryCapabilityImport(
                        "sample.portable-app", interface, "consumer", ("scale",)
                    ),
                ),
            ),
            artifact_export=replace(
                fixture.contract.artifact_export,
                role="library",
                export_id="library",
                media_type="application/vnd.literate-ai.python-package-tree",
            ),
        )
        fixture.contract = provider_contract
        fixture.ports = LocalStandardLifecyclePorts(
            **{**fixture.arguments, "contracts": (provider_contract, root_contract)},
            native_sdk_inputs=fixture.inputs,
            provider_environment={"library": ("FIXTURE_PROVIDER", "library")},
        )
        recorder = QualificationEvidenceRecorder(
            max_bytes=256 * 1024 * 1024, max_records=2000
        )
        fixture.ports.retain_evidence_with(recorder)
        provider_plan, provider_dir, provider_output, provider_manifest = (
            fixture.prepare_standard_package(
                {
                    "consumer.py": (
                        "import vendor_math\ndef scale(value, factor):\n"
                        "    return vendor_math.scale(value, factor)\n"
                    )
                },
                package=False,
            )
        )
        root = copy(fixture)
        root.contract = root_contract
        root.candidate = replace(
            fixture.candidate, component_revision=lock.root_revision
        )
        root.generation = SimpleNamespace(
            component_revision=lock.root_revision,
            direct_generation_edges=(
                SimpleNamespace(
                    provider_revision=provider_contract.component_revision,
                    capability="sample.portable-app",
                    public_interface_identity=interface,
                ),
            ),
        )
        root.provider_artifacts = provider_output.exports
        suite = fixture.ports.source_trees.evidence(
            root.candidate.tree_identity
        ).generated_test_suite
        results = {
            "schema": "literate-ai/generated-test-results@1",
            "cases": [
                {"case_id": name, "outcome": "passed"} for name in suite.case_ids
            ],
        }
        program = (
            "import json,os,sys\nsys.path.insert(0,os.environ['FIXTURE_PROVIDER'])\n"
            "import consumer\nvalue=consumer.scale(1.5,3)\nassert value==4.5\n"
            f"results={results!r}\n"
            "output=results if sys.argv[-1]=='--litai-test' else dict(value=value)\n"
            "print(json.dumps(output))\n"
        )
        root_plan, root_dir, root_output, root_manifest = root.prepare_standard_package(
            program, package=False
        )
        self.assertEqual(root_plan.materialization.native_sdk_input_identities, ())
        command_arguments = dict(
            source_root=fixture.ports.source_trees.resolve(
                root_plan.request.source_tree_identity
            ),
            object_root=fixture.ports.object_root,
            artifact_root=root_dir,
            export_path=root_dir / root_contract.artifact_export.export_id,
            providers=provider_output.exports,
        )
        saved = fixture.ports._plans_by_revision.pop(
            provider_contract.component_revision.uri
        )
        try:
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "finalized build plan"
            ):
                fixture.ports._run_locked(
                    root_contract, ComponentCommandPhase.EXECUTE, **command_arguments
                )
        finally:
            fixture.ports._plans_by_revision[
                provider_contract.component_revision.uri
            ] = saved
        for phase in (ComponentCommandPhase.TEST, ComponentCommandPhase.EXECUTE):
            completed = fixture.ports._run_locked(
                root_contract, phase, **command_arguments
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            if phase is ComponentCommandPhase.EXECUTE:
                self.assertEqual(json.loads(completed.stdout), {"value": 4.5})
            process = {
                "native_sdk_execution_identity": (
                    completed.native_sdk_execution_identity.uri
                )
            }
            retained = QualificationEvidenceReader(
                recorder.entries, max_bytes=256 * 1024 * 1024, max_records=2000
            )
            self.assertEqual(
                sdk_process_fields(
                    retained, plan=root_plan, process=process, phase=phase.value
                ),
                process,
            )
            document = retained.read_json(completed.native_sdk_execution_identity)
            self.assertEqual(
                document["schema"], "literate-ai/native-sdk-command-execution@3"
            )
            changed = deepcopy(document)
            changed["execution_scope"]["plans"] = []
            changed_recorder = QualificationEvidenceRecorder(
                max_bytes=256 * 1024 * 1024, max_records=2000
            )
            for entry in recorder.entries:
                changed_recorder.remember_bytes(entry[1])
            bad = changed_recorder.remember_json(changed)
            with self.assertRaises(QualificationCaptureError):
                sdk_process_fields(
                    QualificationEvidenceReader(
                        changed_recorder.entries,
                        max_bytes=256 * 1024 * 1024,
                        max_records=2000,
                    ),
                    plan=root_plan,
                    process={"native_sdk_execution_identity": bad.uri},
                    phase=phase.value,
                )
        graph = create_artifact_build_graph(
            build_system_driver_identity=root_manifest.build_system_driver_identity,
            manifests=(provider_manifest, root_manifest),
            link_roots=(root_output.exports[0].identity,),
        )
        project = StandardProjectBuildPlan(
            canonical_identity("fixture execution"),
            tuple(
                sorted(
                    (provider_plan, root_plan),
                    key=lambda item: item.component_revision.uri,
                )
            ),
        )
        package_plan, package_result = fixture.ports.create_project_package(
            lock, fixture.execution, project, graph, graph.link_plans[0]
        )
        custody = fixture.ports.project_package_custody(package_plan, package_result)
        scope = custody.native_sdk_execution_scope
        self.assertIsInstance(scope, NativeSdkPackageExecutionScope)
        self.assertEqual(
            scope.input_identities,
            provider_plan.materialization.native_sdk_input_identities,
        )
        self.assertEqual(
            NativeSdkPackageExecutionScope.from_dict(scope.to_dict()), scope
        )
        old_root = custody.root
        relocated = old_root.with_name("relocated package")
        old_root.rename(relocated)
        custody = replace(
            custody,
            root=relocated,
            artifact_paths={
                key: relocated / path.relative_to(old_root)
                for key, path in custody.artifact_paths.items()
            },
        )
        fixture.ports._project_packages[package_result.identity.uri] = custody
        fixture.fixture.fixture.remove_source()
        shutil.rmtree(fixture.service.store.root)
        shutil.rmtree(provider_dir)
        shutil.rmtree(root_dir)
        shutil.rmtree(
            fixture.ports.source_trees.resolve(root_plan.request.source_tree_identity)
        )
        args = (lock, fixture.execution, project, package_plan, package_result)
        tested = fixture.ports.test_root_integration(*args)
        executed = fixture.ports.execute_packaged_project(*args)
        self.assertEqual(
            json.loads(
                fixture.ports.project_execution_stdout[package_result.identity.uri]
            ),
            {"value": 4.5},
        )
        recorder.remember_json(project.identity_document())
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=256 * 1024 * 1024, max_records=2000
        )
        for phase, identity in (("test", tested), ("execute", executed)):
            process = reader.read_json(identity)

            def verify(retained, process=process, phase=phase):
                return packaged_sdk_process_fields(
                    retained,
                    package_plan=package_plan,
                    package_result=package_result,
                    project_build_plan_identity=project.identity,
                    process=process,
                    phase=phase,
                )

            self.assertEqual(
                verify(reader),
                {
                    "native_sdk_execution_identity": process[
                        "native_sdk_execution_identity"
                    ]
                },
            )
            for missing in (
                scope.identity,
                provider_plan.identity,
                provider_contract.identity,
                provider_plan.request.authorization_identity,
            ):
                retained = QualificationEvidenceReader(
                    tuple(entry for entry in recorder.entries if entry[0] != missing),
                    max_bytes=256 * 1024 * 1024,
                    max_records=2000,
                )
                with (
                    self.subTest(missing=missing.uri),
                    self.assertRaises(QualificationCaptureError),
                ):
                    verify(retained)

        # Rehash every enclosing record: corruption checks alone cannot catch a
        # substituted owner's otherwise self-consistent execution scope.
        process = reader.read_json(executed)
        sdk = reader.read_json(
            ContentIdentity.parse_uri(process["native_sdk_execution_identity"])
        )
        package = reader.read_json(
            ContentIdentity.from_dict(sdk["command_binding"]["package_identity"])
        )
        for altered_part in ("missing-plan", "contract", "lock", "authorings"):
            changed = QualificationEvidenceRecorder(
                max_bytes=256 * 1024 * 1024, max_records=2000
            )
            for _, payload in recorder.entries:
                changed.remember_bytes(payload)
            document = scope.to_dict()
            if altered_part == "missing-plan":
                document["plans"] = [
                    p
                    for p in document["plans"]
                    if p["component_revision"] == root_plan.component_revision.to_dict()
                ]
            elif altered_part == "contract":
                document["contracts"][0]["locked_build_authority_identity"] = (
                    canonical_identity("foreign authority").to_dict()
                )
            elif altered_part == "lock":
                document["component_lock"]["resolver_identity"] = canonical_identity(
                    "foreign lock resolver"
                ).to_dict()
            else:
                document["authorings"] = []
            context, execution = deepcopy(package), deepcopy(sdk)
            context["execution_scope"] = changed.remember_json(document).to_dict()
            execution["command_binding"]["package_identity"] = changed.remember_json(
                context
            ).to_dict()
            execution["request"]["builder_id"] = canonical_identity(
                execution["command_binding"]
            ).uri
            execution["authorization"]["request_digest"] = canonical_identity(
                execution["request"]
            ).uri
            changed.remember_json(execution["request"])
            changed.remember_json(execution["authorization"])
            altered = {
                **process,
                "native_sdk_execution_identity": changed.remember_json(execution).uri,
            }
            retained = QualificationEvidenceReader(
                changed.entries, max_bytes=256 * 1024 * 1024, max_records=2000
            )
            with (
                self.subTest(rehashed=altered_part),
                self.assertRaises(QualificationCaptureError),
            ):
                packaged_sdk_process_fields(
                    retained,
                    package_plan=package_plan,
                    package_result=package_result,
                    project_build_plan_identity=project.identity,
                    process=altered,
                    phase="execute",
                )

        # Revocations belong to the linked owner and must still gate the root launch.
        def revoke_before_launch(**kwargs):
            grant = BuildAuthorization(**kwargs)
            fixture.fixture.revocations = fixture.fixture.revocations.revoke(
                grant.authorization_id,
                actor="fixture operator",
                reason="stop linked SDK",
            )
            return grant

        with (
            patch(
                "literate_ai.adapters.lifecycle.standard_local.BuildAuthorization",
                side_effect=revoke_before_launch,
            ),
            patch.object(fixture.ports, "_run_with_environment") as run,
        ):
            with self.assertRaisesRegex(AuthorizationError, "authorization_revoked"):
                fixture.ports.execute_packaged_project(*args)
            run.assert_not_called()
        self.assertEqual(list(fixture.ports.object_root.iterdir()), [relocated.parent])
