"""Invalid or revoked PACKAGE authority cannot instantiate a private adapter."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
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
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.action_package_worker import ConfiguredPackageWorker
from literate_ai.adapters.action_retained_unit_package_worker import (
    retained_unit_package_action,
    retained_unit_package_input,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
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
                    }
                )
            )
            receiver = root / "receiver.py"
            receiver.write_text(
                "import json,sys\n"
                "from pathlib import Path\n"
                "from literate_ai.action_worker import main\n"
                "from literate_ai.adapters.action_retained_unit_package_worker "
                "import ConfiguredRetainedUnitPackageWorker\n"
                "from literate_ai.contracts import ContentIdentity\n"
                "from literate_ai.contracts.execution_dispatch import ExecutionWorker\n"
                "v=json.loads(Path(sys.argv[1]).read_text())\n"
                "i=ContentIdentity.parse_uri(v['identity'])\n"
                "worker=ConfiguredRetainedUnitPackageWorker("
                "ExecutionWorker.from_dict(v['worker']),i,i,i,i,"
                "v['input'].encode(),"
                "lambda context:Path(v['touched']).write_text('ran'),"
                "lambda:None)\n"
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
