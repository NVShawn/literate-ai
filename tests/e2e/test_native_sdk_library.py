"""Independent verifier calls a real packaged native SDK through a library adapter."""

import hashlib
import textwrap
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError, standard_local
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
)
from literate_ai.adapters.native_sdk_qualification import packaged_sdk_process_fields
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    verify_qualification_library_acceptance,
)
from literate_ai.application.standard_project_lifecycle import StandardProjectBuildPlan
from literate_ai.contracts import (
    ComponentCommandPhase,
    ComponentLifecycleCommand,
    LibraryArtifactProduct,
    LibraryCapabilityImport,
    LibraryImportSurface,
    StandardRootIntegrationEvidence,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.security import AuthorizationError, BuildAuthorization
from tests.support import (
    fixtures_test_native_sdk_packaged_execution as test_native_sdk_packaged_execution,
)

_EVIDENCE_BYTE_LIMIT = 256 * 1024 * 1024


def library_contract(contract):
    surface = LibraryImportSurface(
        "python",
        "consumer",
        (
            LibraryCapabilityImport(
                "vendor.math",
                canonical_identity("fixture public interface"),
                "consumer",
                ("scale",),
            ),
        ),
    )
    return replace(
        contract,
        library_import_surface=surface,
        artifact_export=replace(
            contract.artifact_export,
            role="library",
            export_id="library",
            media_type="application/vnd.literate-ai.python-package-tree",
        ),
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
                    "tree",
                    "smoke.py",
                    "--litai-test"
                    if command.phase is ComponentCommandPhase.TEST
                    else "--litai-smoke",
                ),
            )
            for command in contract.commands
        ),
    )


def library_program(results):
    return {
        "consumer.py": (
            "import vendor_math\n"
            "from literate_ai_native_sdk import binding\n"
            "sdk_metadata=binding('vendor_math')\n"
            "assert set(sdk_metadata)=={'sdk_snapshot_identity','target_identity'}\n"
            "def scale(value,factor):\n"
            "    return vendor_math.scale(value,factor)\n"
        ),
        "smoke.py": (
            "import consumer,json,sys\nassert consumer.scale(1.5,3)==4.5\n"
            f"results={results!r}\n"
            "output=results if sys.argv[1]=='--litai-test' else dict(value=4.5)\n"
            "print(json.dumps(output))\n"
        ),
    }


HARNESS = textwrap.dedent("""\
    import importlib,json,sys
    sys.path.insert(0,sys.argv[1])
    consumer=importlib.import_module('consumer')
    cases=json.loads(sys.argv[3])
    print(json.dumps({'schema':'literate-ai/library-acceptance-results@1','cases':[
        {'case_id':case['case_id'],'capability':case['capability'],
         'result':consumer.scale(*case['arguments'])} for case in cases]}))
""").encode()


class NativeSdkLibraryTests(unittest.TestCase):
    def test_native_library_verifier_and_retained_authority_survive_relocation(self):
        helper = test_native_sdk_packaged_execution.NativeSdkPackagedExecutionTests()
        self.addCleanup(helper.doCleanups)
        fixture, plan, custody, resources, recorder, arguments = (
            helper.prepare_relocated_package(
                library_program, contract_factory=library_contract
            )
        )
        lock = arguments[0]
        package_plan, package_result = arguments[-2:]
        self.assertFalse(package_plan.entrypoints)
        node = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        surface = fixture.contract.library_import_surface
        oracle = LibraryAcceptance(
            node.revision.coordinate.name,
            node.revision.specification_set_identity,
            tuple(
                sorted(
                    (item.identity for item in node.revision.public_interfaces),
                    key=lambda item: item.uri,
                )
            ),
            surface.identity,
            "python",
            ContentIdentity.parse_uri("sha256:" + hashlib.sha256(HARNESS).hexdigest()),
            HARNESS,
            (DeclaredLibraryAcceptanceCase("fraction", "vendor.math", [1.5, 3], 4.5),),
        )
        fixture.ports.independent_acceptance_oracle = oracle
        tested = fixture.ports.test_root_integration(*arguments)
        executed = fixture.ports.execute_packaged_project(*arguments)
        project = StandardProjectBuildPlan(
            canonical_identity("fixture project execution"), (plan,)
        )
        recorder.remember_json(project.identity_document())
        grants = []

        def authorize(**kwargs):
            value = BuildAuthorization(**kwargs)
            grants.append(value)
            return value

        def accept():
            return fixture.ports.accept_project_independently(
                *arguments, tested, executed
            )

        with patch.object(standard_local, "BuildAuthorization", side_effect=authorize):
            accepted = accept()
            helper.check_retained(
                recorder,
                project,
                package_plan,
                package_result,
                "library-acceptance",
                accepted,
                library_oracle=oracle,
            )
            reader = QualificationEvidenceReader(
                recorder.entries, max_bytes=_EVIDENCE_BYTE_LIMIT, max_records=2000
            )
            record = reader.read_json(accepted)
            self.assertEqual(
                record["schema"], "literate-ai/local-independent-library-acceptance@2"
            )
            graph = fixture.package_graph
            root = StandardRootIntegrationEvidence(
                component_lock_identity=lock.identity,
                execution_plan_identity=project.execution_plan_identity,
                project_build_plan_identity=project.identity,
                artifact_graph=graph,
                link_plan=graph.link_plans[0],
                package_plan=package_plan,
                package_result=package_result,
                root_generated_integration_test_identity=tested,
                packaged_execution_identity=executed,
                independent_acceptance_identity=accepted,
            )
            export = fixture.ports._planned_exports[lock.root_revision.uri]
            verify_qualification_library_acceptance(
                reader,
                root=root,
                product=LibraryArtifactProduct(export, surface),
                component_lock=lock,
                oracle=oracle,
            )
            sdk = reader.read_json(
                ContentIdentity.parse_uri(record["native_sdk_execution_identity"])
            )
            command_id = ContentIdentity.from_dict(
                sdk["command_binding"]["command_identity"]
            )
            command = reader.read_json(command_id)
            for key in (
                "oracle_identity",
                "harness_identity",
                "driver_identity",
                "toolchain_identity",
            ):
                changed = QualificationEvidenceRecorder(
                    max_bytes=_EVIDENCE_BYTE_LIMIT, max_records=2000
                )
                for _, payload in recorder.entries:
                    changed.remember_bytes(payload)
                altered_command = {
                    **command,
                    key: canonical_identity("foreign verifier").to_dict(),
                }
                execution = deepcopy(sdk)
                execution["command_binding"]["command_identity"] = (
                    changed.remember_json(altered_command).to_dict()
                )
                execution["request"]["builder_id"] = canonical_identity(
                    execution["command_binding"]
                ).uri
                execution["authorization"]["request_digest"] = canonical_identity(
                    execution["request"]
                ).uri
                changed.remember_json(execution["request"])
                changed.remember_json(execution["authorization"])
                altered = {
                    **record,
                    "native_sdk_execution_identity": changed.remember_json(
                        execution
                    ).uri,
                }
                retained = QualificationEvidenceReader(
                    changed.entries,
                    max_bytes=_EVIDENCE_BYTE_LIMIT,
                    max_records=2000,
                )
                with (
                    self.subTest(binding=key),
                    self.assertRaises(QualificationCaptureError),
                ):
                    packaged_sdk_process_fields(
                        retained,
                        package_plan=package_plan,
                        package_result=package_result,
                        project_build_plan_identity=project.identity,
                        process=altered,
                        phase="library-acceptance",
                        library_oracle=oracle,
                    )
            for supplied in (
                None,
                replace(
                    oracle,
                    harness_content=HARNESS + b"# changed\n",
                    harness_identity=ContentIdentity.parse_uri(
                        "sha256:" + hashlib.sha256(HARNESS + b"# changed\n").hexdigest()
                    ),
                ),
                replace(
                    oracle, cases=(replace(oracle.cases[0], expected_result=9.25),)
                ),
            ):
                with (
                    self.subTest(oracle=supplied),
                    self.assertRaises(QualificationCaptureError),
                ):
                    packaged_sdk_process_fields(
                        reader,
                        package_plan=package_plan,
                        package_result=package_result,
                        project_build_plan_identity=project.identity,
                        process=record,
                        phase="library-acceptance",
                        library_oracle=supplied,
                    )
            fixture.ports.independent_acceptance_oracle = replace(
                oracle, cases=(replace(oracle.cases[0], expected_result=9.25),)
            )
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "differs for 'fraction'"
            ):
                accept()
            changed = (
                HARNESS + b"\nfrom pathlib import Path\n"
                b"Path(__file__).write_bytes(b'changed')\n"
            )
            fixture.ports.independent_acceptance_oracle = replace(
                oracle,
                harness_content=changed,
                harness_identity=ContentIdentity.parse_uri(
                    "sha256:" + hashlib.sha256(changed).hexdigest()
                ),
            )
            with self.assertRaisesRegex(LocalStandardLifecycleError, "harness changed"):
                accept()
            fixture.ports.independent_acceptance_oracle = oracle
            original_run = standard_local.run_with_tree_kill
            launched = []

            def revoke_after_run(command, **kwargs):
                launched.extend(
                    Path(item) for item in command if Path(item).name == "harness.py"
                )
                result = original_run(command, **kwargs)
                fixture.fixture.revocations = fixture.fixture.revocations.revoke(
                    grants[-1].authorization_id,
                    actor="fixture operator",
                    reason="revoke verifier",
                )
                return result

            with patch.object(
                standard_local, "run_with_tree_kill", side_effect=revoke_after_run
            ):
                with self.assertRaisesRegex(
                    AuthorizationError, "authorization_revoked"
                ):
                    accept()
            self.assertTrue(launched)
            self.assertTrue(all(not item.exists() for item in launched))
        resources.verify_materialized(custody.root)
        self.assertEqual(
            list(fixture.ports.object_root.iterdir()), [custody.root.parent]
        )
