"""Shared fixtures extracted from ``tests.unit.test_native_sdk_packaged_execution``."""

import json
import shutil
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.lifecycle import (
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
)
from literate_ai.adapters.native_sdk_qualification import packaged_sdk_process_fields
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.application.standard_project_lifecycle import StandardProjectBuildPlan
from literate_ai.contracts.executable_components.commands import (
    ComponentCommandPhase,
    ComponentLifecycleCommand,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from tests.support import (
    fixtures_test_native_sdk_standard_authority as test_native_sdk_standard_authority,
)


class NativeSdkPackagedExecutionTests(unittest.TestCase):
    def prepare_relocated_package(
        self, program_factory=None, entrypoint_kind=None, contract_factory=None
    ):
        fixture = test_native_sdk_standard_authority.NativeSdkStandardAuthorityTests()
        self.addCleanup(fixture.doCleanups)
        fixture.entrypoint_kind = entrypoint_kind
        fixture.setUp()
        fixture.contract = replace(
            fixture.contract,
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
                        "--litai-test"
                        if command.phase is ComponentCommandPhase.TEST
                        else "--litai-smoke",
                    ),
                )
                for command in fixture.contract.commands
            ),
        )
        if contract_factory is not None:
            fixture.contract = contract_factory(fixture.contract)
        fixture.ports = LocalStandardLifecyclePorts(
            **{**fixture.arguments, "contracts": (fixture.contract,)},
            native_sdk_inputs=fixture.inputs,
        )
        recorder = QualificationEvidenceRecorder(
            max_bytes=256 * 1024 * 1024, max_records=2000
        )
        fixture.ports.retain_evidence_with(recorder)
        suite = fixture.ports.source_trees.evidence(
            fixture.candidate.tree_identity
        ).generated_test_suite
        results = {
            "schema": "literate-ai/generated-test-results@1",
            "cases": [
                {"case_id": name, "outcome": "passed"} for name in suite.case_ids
            ],
        }
        program = (
            "import json,sys,vendor_math\n"
            "value=vendor_math.scale(1.5,3)\nassert value==4.5\n"
            f"results={results!r}\n"
            "output=results if sys.argv[-1]=='--litai-test' else dict(value=value)\n"
            "print(json.dumps(output))\n"
        )
        if program_factory is not None:
            program = program_factory(results)
        plan, artifact, package_plan, package_result, custody, resources = (
            fixture.prepare_standard_package(program)
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
        shutil.rmtree(artifact)
        source = fixture.ports.source_trees.resolve(plan.request.source_tree_identity)
        shutil.rmtree(source)
        arguments = (
            fixture.fixture.snapshot.authority.lock,
            fixture.execution,
            None,
            package_plan,
            package_result,
        )
        return fixture, plan, custody, resources, recorder, arguments

    def test_standard_package_runs_after_producer_deletion_and_relocation(self):
        fixture, plan, custody, resources, recorder, arguments = (
            self.prepare_relocated_package()
        )
        package_plan, package_result = arguments[-2:]
        relocated = custody.root
        tested = fixture.ports.test_root_integration(*arguments)
        executed = fixture.ports.execute_packaged_project(*arguments)
        self.assertNotEqual(tested, executed)
        self.assertEqual(
            json.loads(
                fixture.ports.project_execution_stdout[package_result.identity.uri]
            ),
            {"value": 4.5},
        )
        resources.verify_materialized(relocated)
        project = StandardProjectBuildPlan(
            canonical_identity("fixture execution plan"), (plan,)
        )
        recorder.remember_json(project.identity_document())
        for phase, identity in (("test", tested), ("execute", executed)):
            self.check_retained(
                recorder, project, package_plan, package_result, phase, identity
            )
        path = relocated / resources.inputs[0].path
        original = path.read_bytes()
        path.write_bytes(original + b"changed")
        with patch.object(fixture.ports, "_run_with_environment") as run:
            with self.assertRaises(LocalStandardLifecycleError):
                fixture.ports.execute_packaged_project(*arguments)
            run.assert_not_called()
        path.write_bytes(original)
        self.assertEqual(list(fixture.ports.object_root.iterdir()), [relocated.parent])

    def check_retained(
        self,
        recorder,
        project,
        package_plan,
        package_result,
        phase,
        identity,
        library_oracle=None,
    ):
        def reader(entries=None):
            return QualificationEvidenceReader(
                recorder.entries if entries is None else entries,
                max_bytes=256 * 1024 * 1024,
                max_records=2000,
            )

        process = reader().read_json(identity)

        def verify(process, retained):
            return packaged_sdk_process_fields(
                retained,
                package_plan=package_plan,
                package_result=package_result,
                project_build_plan_identity=project.identity,
                process=process,
                phase=phase,
                library_oracle=library_oracle,
            )

        self.assertEqual(
            verify(process, reader()),
            {"native_sdk_execution_identity": process["native_sdk_execution_identity"]},
        )
        sdk_identity = ContentIdentity.parse_uri(
            process["native_sdk_execution_identity"]
        )
        sdk = reader().read_json(sdk_identity)
        package_identity = ContentIdentity.from_dict(
            sdk["command_binding"]["package_identity"]
        )
        package = reader().read_json(package_identity)
        for missing in (
            sdk_identity,
            package_identity,
            project.identity,
            ContentIdentity.from_dict(package["tree_identity"]),
            ContentIdentity.from_dict(package["component_build_plan"]),
        ):
            retained = reader(
                tuple(item for item in recorder.entries if item[0] != missing)
            )
            with (
                self.subTest(missing=missing.uri),
                self.assertRaises(QualificationCaptureError),
            ):
                verify(process, retained)
        for key in (
            "package_plan",
            "package_result",
            "component_build_plan",
            "tree_identity",
        ):
            changed = QualificationEvidenceRecorder(
                max_bytes=256 * 1024 * 1024, max_records=2000
            )
            for _, payload in recorder.entries:
                changed.remember_bytes(payload)
            context, execution = deepcopy(package), deepcopy(sdk)
            context[key] = canonical_identity("foreign package scope").to_dict()
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
            retained = reader(changed.entries)
            with (
                self.subTest(changed=key),
                self.assertRaises(QualificationCaptureError),
            ):
                verify(altered, retained)
