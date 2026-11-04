"""Invalid or revoked PACKAGE authority cannot instantiate a private adapter."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import tests.support.fixtures_test_action_package as fixture_module
from literate_ai.adapters.action_capabilities import (
    CAPABILITY_REQUEST_SCHEMA,
    decode_capability_response,
    receiver_code_identity,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    decode_attested_action_response,
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.action_package_worker import ConfiguredPackageWorker
from literate_ai.adapters.action_retained_unit_package_worker import (
    retained_unit_package_action,
    retained_unit_package_input,
    retained_unit_verification_input,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import BlobRef, canonical_identity, canonical_json_bytes
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.storage.cas import FileSystemCAS


class ConfiguredPackageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.PackageActionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.factory = Mock()
        self.guard = Mock()
        self.worker = ConfiguredPackageWorker(
            f.fixture.value.plan.packager_identity, self.factory, self.guard
        )

    def execute(self, **changes):
        f = self.fixture
        args = dict(
            expected_worker_identity=f.request.worker.worker_identity,
            cas=Mock(),
            admission_guard=lambda: None,
        )
        args.update(changes)
        return self.worker.execute(f.request, f.fixture.deadline, f.records, **args)

    def test_wrong_receiver_rejects_before_adapter_creation(self):
        with self.assertRaises(ActionWireError):
            self.execute(expected_worker_identity=canonical_identity("other-worker"))
        self.factory.assert_not_called()

    def test_revoked_private_configuration_rejects_before_adapter_creation(self):
        self.guard.side_effect = ActionWireError(
            "fixture.revoked", "configuration changed"
        )
        with self.assertRaises(ActionWireError) as error:
            self.execute()
        self.assertEqual(error.exception.code, "fixture.revoked")
        self.factory.assert_not_called()

    def test_cancelled_dispatch_rejects_before_adapter_creation(self):
        with self.assertRaises(ActionWireError) as error:
            self.execute(cancelled=lambda: True)
        self.assertEqual(error.exception.code, "action_package.cancelled")
        self.factory.assert_not_called()

    def test_wrong_packager_rejects_before_adapter_creation(self):
        self.worker = ConfiguredPackageWorker(
            canonical_identity("other-packager"), self.factory, self.guard
        )
        with self.assertRaises(ActionWireError) as error:
            self.execute()
        self.assertEqual(error.exception.code, "action_package.packager_mismatch")
        self.factory.assert_not_called()


class RetainedPackageReceiverScopeTests(unittest.TestCase):
    def test_private_package_receiver_rejects_other_actions_before_generic_dispatch(
        self,
    ):
        """A direct wire caller cannot broaden a retained execution grant."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            cas = FileSystemCAS(root / "cas")
            workspace = root / "workspace"
            workspace.mkdir()
            touched = root / "executed"
            authority = root / "authority"
            authority.write_bytes(b"approved")
            worker = ExecutionWorker(
                "retained-receiver",
                ExecutionWorkerKind.SSH,
                endpoint="fixture@127.0.0.1",
                # Remote descriptor paths use POSIX syntax on every controller.
                # The receiver's actual local storage is supplied separately.
                workspace="/retained-workspace",
            )
            identity = canonical_identity("retained-scope-fixture")
            intent = retained_unit_package_input({"authorization": identity.uri})
            settings = root / "private.json"
            settings.write_text(
                json.dumps(
                    {
                        "worker": worker.to_dict(),
                        "identity": identity.uri,
                        "input": intent.decode(),
                        "cas": str(cas.root),
                        "workspace": str(workspace),
                        "touched": str(touched),
                        "authority": str(authority),
                        "custom_measurement": False,
                    }
                )
            )
            receiver = root / "receiver.py"
            receiver.write_text(
                "import json,sys,time\n"
                "from pathlib import Path\n"
                "from literate_ai.action_worker import main\n"
                "from literate_ai.adapters.action_retained_unit_package_worker "
                "import ConfiguredRetainedUnitPackageWorker\n"
                "from literate_ai.adapters.retained_package_capability "
                "import RegisteredRetainedPackageCapabilityProbe\n"
                "from literate_ai.adapters.retained_unit_verification "
                "import RetainedUnitVerificationSelection\n"
                "from literate_ai.contracts import ContentIdentity\n"
                "from literate_ai.contracts.execution_dispatch import ExecutionWorker\n"
                "v=json.loads(Path(sys.argv[1]).read_text())\n"
                "registered=Path(sys.argv[1]).read_bytes()\n"
                "def registered_current():\n"
                " if Path(sys.argv[1]).read_bytes()!=registered:"
                "raise RuntimeError('registration changed')\n"
                "def underlying_revocation():\n"
                " raise ValueError('private underlying marker')\n"
                "def full_current():\n"
                " registered_current()\n"
                " if Path(v['authority']).read_bytes()!=b'approved':\n"
                "  try:underlying_revocation()\n"
                "  except ValueError as exc:"
                "raise RuntimeError('authority changed') from exc\n"
                " if v.get('describe_barrier'):\n"
                "  barrier=Path(v['describe_barrier'])\n"
                "  ready=barrier.with_suffix('.ready')\n"
                "  if not ready.exists():\n"
                "   ready.write_text('checked')\n"
                "   end=time.monotonic()+10\n"
                "   while not barrier.exists():\n"
                "    if time.monotonic()>end:raise RuntimeError('barrier expired')\n"
                "    time.sleep(.01)\n"
                "def run(context):\n"
                " Path(v['touched']).write_text('entered')\n"
                " context.require_current()\n"
                " Path(v['authority']).write_bytes(b'changed')\n"
                " context.require_current()\n"
                " Path(v['touched']).write_text('unsafe completion')\n"
                " raise RuntimeError('revoked authority reached completion')\n"
                "class CustomMeasurementsWorker(ConfiguredRetainedUnitPackageWorker):\n"
                " def execute(self,*args,**kwargs):\n"
                "  observed=kwargs['measure_capability']()\n"
                "  class PretendProbe(RegisteredRetainedPackageCapabilityProbe):\n"
                "   def __init__(self):pass\n"
                "   def require_binding(self,*args):pass\n"
                "   def require_registered_current(self):pass\n"
                "   def __call__(self):return observed\n"
                "  kwargs['measure_capability']=(PretendProbe() "
                "if v['custom_measurement']=='subclass' else lambda:observed)\n"
                "  return super().execute(*args,**kwargs)\n"
                "i=ContentIdentity.parse_uri(v['identity'])\n"
                "Worker=CustomMeasurementsWorker if v['custom_measurement'] "
                "else ConfiguredRetainedUnitPackageWorker\n"
                "worker=Worker("
                "ExecutionWorker.from_dict(v['worker']),i,i,i,i,"
                "v['input'].encode(),"
                "run,full_current,poll_guard=registered_current,"
                "verification_selection=(RetainedUnitVerificationSelection.from_dict("
                "v['verification_selection']) if v.get('verification_selection') "
                "else None))\n"
                "raise SystemExit(main(['--cas',v['cas'],'--workspace',v['workspace'],"
                "*sys.argv[2:]],package_worker=worker))\n"
            )
            command = [sys.executable, str(receiver), str(settings)]
            environment = {
                **os.environ,
                "LITAI_ACTION_WORKER_IDENTITY": worker.identity.uri,
            }
            deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=60))
            challenge = canonical_json_bytes(
                {
                    "schema": CAPABILITY_REQUEST_SCHEMA,
                    "worker_identity": worker.identity.uri,
                    "nonce": secrets.token_hex(16),
                    "deadline": deadline.to_dict(),
                }
            )
            described = subprocess.run(
                [*command, "--describe"],
                input=challenge,
                capture_output=True,
                env=environment,
                timeout=30,
            )
            self.assertEqual(described.returncode, 0, described.stderr)
            observed = decode_capability_response(
                described.stdout, challenge, receiver_code_identity()
            )
            self.assertEqual(observed.actions, (LifecycleActionKind.PACKAGE,))
            # A real private input change after the entry check must withhold
            # capability bytes at the closing publication check.
            original_settings = settings.read_bytes()
            for changed_path in (authority, settings):
                with self.subTest(describe_mutation=changed_path.name):
                    barrier = root / (changed_path.name + ".release")
                    private = json.loads(original_settings)
                    private["describe_barrier"] = str(barrier)
                    settings.write_text(json.dumps(private))
                    process = subprocess.Popen(
                        [*command, "--describe"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=environment,
                    )
                    try:
                        process.stdin.write(challenge)
                        process.stdin.close()
                        process.stdin = None
                        end = time.monotonic() + 10
                        while not barrier.with_suffix(".ready").exists():
                            if process.poll() is not None or time.monotonic() >= end:
                                self.fail(
                                    "real receiver did not reach publication barrier"
                                )
                            time.sleep(0.01)
                        changed_path.write_bytes(changed_path.read_bytes() + b" ")
                        barrier.write_text("continue")
                        stdout, stderr = process.communicate(timeout=15)
                        self.assertEqual(process.returncode, 2, stderr)
                        self.assertEqual(
                            json.loads(stdout)["schema"],
                            "literate-ai/action-observation-failure@1",
                        )
                        with self.assertRaises(ActionWireError):
                            decode_capability_response(
                                stdout, challenge, receiver_code_identity()
                            )
                        self.assertFalse(touched.exists())
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=5)
                        authority.write_bytes(b"approved")
                        settings.write_bytes(original_settings)
            route = LifecycleActionWorker(
                worker.worker_id, worker.identity, identity, observed.identity
            )
            payload = record_identity(intent)
            node = retained_unit_package_action(identity, worker.worker_id, payload)
            before = tuple(cas.iter_refs())
            for changed in (
                replace(node, kind=LifecycleActionKind.INDEX),
                replace(node, action_id="unregistered-package-action"),
            ):
                with self.subTest(kind=changed.kind, action=changed.action_id):
                    request = LifecycleActionDispatchRequest(
                        identity, changed, route, 0, (), deadline.identity, (payload,)
                    )
                    result = subprocess.run(
                        command,
                        input=encode_action_request(
                            request,
                            deadline,
                            {payload: intent},
                            capability=observed.capability_identity,
                        ),
                        capture_output=True,
                        env=environment,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, b"")
                    self.assertFalse(touched.exists())
                    self.assertEqual(tuple(cas.iter_refs()), before)
                    self.assertEqual(tuple(workspace.iterdir()), ())
            # Matching cached capability data from a custom callback cannot
            # replace full authority freshness. Both real receiver paths refuse
            # an authority change made during the registered action.
            for custom in (False, True, "subclass"):
                with self.subTest(custom_measurement=custom):
                    private = json.loads(settings.read_text())
                    private["custom_measurement"] = custom
                    settings.write_text(json.dumps(private))
                    authority.write_bytes(b"approved")
                    if touched.exists():
                        touched.unlink()
                    request = LifecycleActionDispatchRequest(
                        identity, node, route, 0, (), deadline.identity, (payload,)
                    )
                    result = subprocess.run(
                        command,
                        input=encode_action_request(
                            request,
                            deadline,
                            {payload: intent},
                            capability=observed.capability_identity,
                        ),
                        capture_output=True,
                        env=environment,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    outcome, record, attested = decode_attested_action_response(
                        result.stdout, request
                    )
                    self.assertEqual(
                        outcome.failure_code, "action_source.custody_unavailable"
                    )
                    self.assertIsNone(outcome.result_identity)
                    self.assertIsNone(record)
                    self.assertIsNone(attested)
                    diagnostic = json.loads(result.stderr.splitlines()[-1])
                    self.assertEqual(
                        diagnostic["request_identity"], request.identity.uri
                    )
                    self.assertEqual(diagnostic["exception_type"], "RuntimeError")
                    self.assertTrue(diagnostic["frames"])
                    self.assertIn(
                        "underlying_revocation",
                        [frame["function"] for frame in diagnostic["frames"]],
                    )
                    self.assertNotIn(b"private underlying marker", result.stderr)
                    self.assertNotIn(str(root).encode(), result.stderr)
                    self.assertNotIn(b"authority changed", result.stderr)
                    self.assertEqual(touched.read_text(), "entered")
                    self.assertEqual(tuple(cas.iter_refs()), before)
                    self.assertEqual(tuple(workspace.iterdir()), ())
            # A real verification receiver must refuse both an execution-family
            # grant and a different original index before entering its action.
            selection = {
                "verification_mode": "fresh-exact-projection",
                "result_index": BlobRef(
                    "a" * 64, 100, media_type="application/json"
                ).to_dict(),
                "source_custody_identity": canonical_identity("private-source").uri,
            }
            verification = {
                "authority_mode": "retained-project",
                "operation": "verify",
                **{
                    name: identity.uri
                    for name in (
                        "retained_plan_identity",
                        "worker_selection_identity",
                        "policy_identity",
                        "native_tool_observations_identity",
                    )
                },
                "requires_host_execution_authorization": True,
                "release_authorized": False,
                **selection,
            }
            verification_input = retained_unit_verification_input(verification)
            private = json.loads(settings.read_text())
            private.update(
                input=verification_input.decode(),
                verification_selection=selection,
                custom_measurement=False,
            )
            settings.write_text(json.dumps(private))
            authority.write_bytes(b"approved")
            if touched.exists():
                touched.unlink()
            described = subprocess.run(
                [*command, "--describe"],
                input=challenge,
                capture_output=True,
                env=environment,
                timeout=30,
            )
            self.assertEqual(described.returncode, 0, described.stderr)
            measured = decode_capability_response(
                described.stdout, challenge, receiver_code_identity()
            )
            self.assertNotEqual(
                measured.capability_identity, observed.capability_identity
            )
            verification_route = replace(route, observation_identity=measured.identity)
            changed_index = {
                **verification,
                "result_index": BlobRef(
                    "b" * 64, 100, media_type="application/json"
                ).to_dict(),
            }
            for substituted in (
                retained_unit_package_input({**verification, "operation": "execute"}),
                retained_unit_verification_input(changed_index),
            ):
                substituted_identity = record_identity(substituted)
                request = LifecycleActionDispatchRequest(
                    identity,
                    retained_unit_package_action(
                        identity, worker.worker_id, substituted_identity
                    ),
                    verification_route,
                    0,
                    (),
                    deadline.identity,
                    (substituted_identity,),
                )
                result = subprocess.run(
                    command,
                    input=encode_action_request(
                        request,
                        deadline,
                        {substituted_identity: substituted},
                        capability=measured.capability_identity,
                    ),
                    capture_output=True,
                    env=environment,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertFalse(touched.exists())
                self.assertEqual(tuple(cas.iter_refs()), before)
                self.assertEqual(tuple(workspace.iterdir()), ())
