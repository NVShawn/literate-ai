"""Real SDK imports cross Standard's fresh, revocable command boundary."""

from __future__ import annotations

import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
    direct_service_process_argv,
)
from literate_ai.adapters.native_sdk_execution import prepare_native_sdk_execution
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.contracts.executable_components.commands import (
    ComponentCommandPhase,
    ComponentLifecycleCommand,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.security import AuthorizationError, BuildAuthorization, SecurityProfile
from tests.support import (
    fixtures_test_native_sdk_standard_authority as test_native_sdk_standard_authority,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.unit.native_sdk_qualification_checks import check_sdk_records


class NativeSdkExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = (
            test_native_sdk_standard_authority.NativeSdkStandardAuthorityTests()
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.inputs = self.fixture.inputs
        original = self.fixture.contract
        commands = tuple(
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
                    "--litai-smoke",
                ),
            )
            for command in original.commands
        )
        self.contract = replace(original, commands=commands)
        self.ports = LocalStandardLifecyclePorts(
            **{**self.fixture.arguments, "contracts": (self.contract,)},
            native_sdk_inputs=self.inputs,
        )
        intent = self.fixture.intent(self.ports)
        self.plan = self.ports.finalize(
            intent,
            self.ports.authorize(intent, canonical_identity("fixture source index")),
        )
        self.root = self.ports.object_root / "fixture-consumer"
        self.root.mkdir()
        self.app = self.root / "app"
        self.app.write_text(
            "import json,os,sys,vendor_math\n"
            "from literate_ai_native_sdk import binding\n"
            "metadata=binding('vendor_math')\n"
            "try: metadata['sdk_snapshot_identity']='forged'\n"
            "except TypeError: pass\n"
            "else: raise AssertionError('SDK metadata is mutable')\n"
            "for missing in ('absent-package','',None):\n"
            "    try: binding(missing)\n"
            "    except LookupError: pass\n"
            "    else: raise AssertionError('missing SDK metadata was supplied')\n"
            "print(json.dumps(dict(value=vendor_math.scale(1.5,3),pid=os.getpid(),"
            "mode=sys.argv[-1],metadata=dict(metadata))))\n"
        )
        (self.root / "literate_ai_native_sdk.py").write_text(
            "raise AssertionError('ambient SDK metadata module was imported')\n"
        )

    def test_standard_command_runs_real_sdk_and_preserves_service_import_driver(self):
        recorder = QualificationEvidenceRecorder(max_bytes=20_000_000, max_records=2000)
        self.ports.retain_evidence_with(recorder)
        intent = self.fixture.intent(self.ports)
        self.plan = self.ports.finalize(
            intent,
            self.ports.authorize(intent, canonical_identity("fixture source index")),
        )
        wire = self.contract.to_dict()
        SchemaCatalog().validate(wire["schema"], wire)
        self.assertEqual(type(self.contract).from_dict(wire), self.contract)
        source = self.ports.source_trees.resolve(self.plan.request.source_tree_identity)
        with patch.object(
            self.ports, "_record_evidence", wraps=self.ports._record_evidence
        ) as record:
            for phase in (ComponentCommandPhase.TEST, ComponentCommandPhase.EXECUTE):
                result = self.ports._run_locked(
                    self.contract,
                    phase,
                    source_root=source,
                    object_root=self.root,
                    artifact_root=self.root,
                    export_path=self.app,
                    providers=(),
                )
                value = json.loads(result.stdout)
                self.assertEqual(value["value"], 4.5)
                self.assertEqual(value["mode"], "--litai-smoke")
                self.assertGreater(value["pid"], 0)
                (sdk,) = self.inputs.for_consumer(
                    self.contract.component_revision,
                    target_identity=self.contract.artifact_export.target_identity,
                )
                snapshot = sdk.build.product.snapshot
                self.assertEqual(
                    value["metadata"],
                    {
                        "sdk_snapshot_identity": snapshot.identity.uri,
                        "target_identity": snapshot.target_identity.uri,
                    },
                )
                process_identity = self.ports._process_observation(
                    result, phase=phase.value, plan_identity=self.plan.identity
                )
                process = QualificationEvidenceReader(
                    recorder.entries, max_bytes=20_000_000, max_records=2000
                ).read_json(process_identity)
                check_sdk_records(
                    self, recorder, self.plan, phase.value, process, self.contract
                )
        documents = [call.args[0] for call in record.call_args_list]
        executions = [
            item
            for item in documents
            if isinstance(item, dict)
            and item.get("schema") == "literate-ai/native-sdk-command-execution@2"
        ]
        self.assertEqual(len(executions), 2)
        self.assertNotEqual(executions[0]["request"], executions[1]["request"])
        self.assertEqual(
            executions[0]["runtime_identity"], executions[1]["runtime_identity"]
        )
        self.assertEqual(
            sum(
                isinstance(item, dict) and "native_sdk_execution_identity" in item
                for item in documents
            ),
            2,
        )
        argv = (
            "python",
            "-I",
            "-c",
            STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
            "manifest",
            "artifact",
            "export",
            "file",
            "app",
            "--litai-smoke",
        )
        self.assertEqual(
            direct_service_process_argv(argv), (*argv[:-1], "--litai-serve")
        )
        self.assertEqual(list(self.ports.object_root.iterdir()), [self.root])

    def test_stale_grants_manifest_graph_and_revocation_refuse_execution(self):
        now = datetime.now(UTC)
        grants = []

        def authorize(request, runtime):
            grant = BuildAuthorization(
                authorization_id=f"test-sdk:{canonical_identity(request.to_dict()).digest}",
                classification_digest=runtime.uri,
                request_digest=canonical_identity(request.to_dict()).uri,
                effective_revision_digest=request.effective_revision_digest,
                actor="fixture-operator",
                reason="Exercise the exact native SDK fixture",
                profile=SecurityProfile.CONSTRAINED,
                privileges=request.requested_privileges,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            grants.append(grant)
            return grant

        tool = next(iter(self.ports.tool_bindings.values()))
        self.assertIsInstance(tool, LocalComponentToolBinding)
        execute = Mock(return_value="fixture observation")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            not in {"PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"}
            and not key.upper().startswith("DYLD_")
        }
        with prepare_native_sdk_execution(
            self.inputs,
            self.contract.component_revision,
            target=self.contract.artifact_export.target_identity,
            parent=self.ports.object_root,
        ) as inputs:
            arguments = dict(
                tool=tool,
                cwd=self.root,
                environment=environment,
                source_identity=self.plan.request.source_tree_identity,
                phase="test",
                command_identity=canonical_identity("fixture command"),
                command_contract_identity=self.contract.identity,
                authorize=authorize,
                clock=lambda: now,
                execute=execute,
                record=canonical_identity,
            )
            argv = (*tool.command, "-I", "-c", "pass")
            inputs.run(argv, **arguments)
            self.assertEqual(execute.call_count, 1)
            original_grant = grants[0]
            source_grant = next(iter(self.fixture.service._grants.values()))
            with self.assertRaisesRegex(ValueError, "fresh runtime graph"):
                inputs.run(argv, **{**arguments, "authorize": lambda *_: source_grant})
            with self.assertRaisesRegex(AuthorizationError, "privilege_mismatch"):
                inputs.run(
                    argv,
                    **{
                        **arguments,
                        "authorize": lambda request, runtime: replace(
                            authorize(request, runtime), privileges=()
                        ),
                    },
                )
            with self.assertRaises(AuthorizationError):
                inputs.run(
                    (*argv, "changed"),
                    **{**arguments, "authorize": lambda *_: original_grant},
                )
            with self.assertRaises(AuthorizationError):
                inputs.run(
                    argv, **{**arguments, "clock": lambda: now + timedelta(minutes=6)}
                )
            with self.assertRaisesRegex(ValueError, "ambient import or loader"):
                inputs.run(
                    argv,
                    **{
                        **arguments,
                        "environment": {**environment, "LD_PRELOAD": "foreign"},
                    },
                )
            original = inputs.manifest.read_bytes()
            inputs.manifest.write_bytes(b"changed manifest")
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                inputs.run(argv, **arguments)
            inputs.manifest.write_bytes(original)

            def authorize_and_change(request, runtime):
                grant = authorize(request, runtime)
                inputs.manifest.write_bytes(b"changed after authorization")
                return grant

            with self.assertRaisesRegex(ValueError, "manifest changed"):
                inputs.run(argv, **{**arguments, "authorize": authorize_and_change})
            inputs.manifest.write_bytes(original)
            value = inputs.values[0]
            sdk = value.binding.build.product.snapshot
            library = value.root / sdk.native_libraries[0]
            library_bytes = library.read_bytes()
            library.write_bytes(library_bytes + b"changed")
            try:
                with self.assertRaisesRegex(ValueError, "SDK bytes changed"):
                    inputs.run(argv, **arguments)
            finally:
                library.write_bytes(library_bytes)
            stale = replace(value.dependencies, _components_json=b"[]")
            changed = replace(inputs, values=(replace(value, dependencies=stale),))
            with self.assertRaisesRegex(ValueError, "runtime dependencies changed"):
                changed.run(argv, **arguments)
            original_revocations = self.fixture.fixture.revocations
            self.fixture.fixture.revocations = original_revocations.revoke(
                original_grant.authorization_id,
                actor="operator",
                reason="stop the fixture",
            )
            with self.assertRaises(AuthorizationError):
                inputs.run(argv, **arguments)
            self.assertEqual(execute.call_count, 1)
            self.fixture.fixture.revocations = original_revocations

            def execute_and_revoke(*_):
                self.fixture.fixture.revocations = original_revocations.revoke(
                    grants[-1].authorization_id,
                    actor="operator",
                    reason="stop after launch",
                )
                return "unaccepted observation"

            with self.assertRaises(AuthorizationError):
                inputs.run(argv, **{**arguments, "execute": execute_and_revoke})
        self.assertFalse(inputs.manifest.exists())
