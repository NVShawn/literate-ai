"""Actual selector transport verifies private resolution and challenged authority."""

import json
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_capabilities import (
    MAX_CAPABILITY_BYTES,
    run_command_observation,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_hardware import probe_command_hardware
from literate_ai.adapters.action_tool_selectors import (
    decode_selector_request,
    decode_selector_response,
    probe_command_tool_selectors,
)
from literate_ai.adapters.remote_standard_toolchains import RemoteStandardToolchains
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import (
    ToolchainConstraint,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog
from tests.unit import test_action_tool_observation as fixture


class ActionToolSelectorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ActionToolObservationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.shadow = self.fixture.root / "shadow"
        self.shadow.mkdir()
        receiver = self.fixture.root / "receiver.py"
        receiver.write_text(
            receiver.read_text().replace(
                "environment=dict(os.environ)",
                "environment=dict(os.environ, PATH="
                + repr(str(self.shadow))
                + "+os.pathsep+os.path.dirname(sys.executable), "
                "PATHEXT=os.environ.get('PATHEXT','.EXE;.CMD'))",
            )
        )
        self.selector = {"python": (Path(sys.executable).name,)}

    def observed(self):
        return self.fixture.probe(self.fixture.admission())

    def probe(self, observed, selectors=None):
        return probe_command_tool_selectors(
            self.fixture.worker,
            observed,
            self.fixture.deadline,
            self.selector if selectors is None else selectors,
            cwd=self.fixture.root,
            environment=self.fixture.environment,
        )

    def test_real_stdin_request_file_and_fresh_challenges_preserve_selector_identity(
        self,
    ):
        first = self.observed()
        proof = self.probe(first)
        self.assertEqual(proof, self.probe(first))
        self.fixture.worker = replace(
            self.fixture.worker,
            command=(*self.fixture.worker.command, "--request-file", "{request_file}"),
        )
        self.probe(self.observed())
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_new_path_shadow_refuses_without_changing_registered_inventory(self):
        observed = self.observed()
        self.probe(observed)
        shadow = self.shadow / Path(sys.executable).name
        shadow.write_bytes(b"must never execute this selector")
        shadow.chmod(0o755)
        self.assertEqual(self.observed().tools, observed.tools)
        with self.assertRaises(ActionWireError):
            self.probe(observed)
        with patch(
            "literate_ai.adapters.action_tool_selectors.run_command_observation"
        ) as transport:
            with self.assertRaises(ActionWireError):
                self.probe(observed, {"missing": ("tool",)})
            transport.assert_not_called()

    def test_admitted_consumer_verifies_alias_and_rechecks_private_path_resolution(
        self,
    ):
        fixture = self.fixture
        fixture.environment["LITAI_ACTION_WORKER_IDENTITY"] = (
            fixture.worker.identity.uri
        )
        hardware = probe_command_hardware(
            fixture.worker,
            timeout_seconds=60,
            cwd=fixture.root,
            environment=fixture.environment,
        )
        catalog = ExecutionWorkerCatalog((fixture.worker,))
        pool = CommandActionWorkerPool(
            lambda: catalog,
            lambda: WorkerHardwareObservationCatalog((hardware,)),
            lambda worker: canonical_identity({"healthy": worker.identity.uri}),
            fixture.deadline,
            phase=LifecycleActionKind.INDEX,
            source_handoff="filesystem-cas",
            target_profile="host",
            cwd=fixture.root,
            environment=fixture.environment,
        )
        consumer = RemoteStandardToolchains.from_admission(pool, pool.workers[0])
        with patch("shutil.which", side_effect=AssertionError("controller PATH")):
            tool = consumer.discover(
                "python",
                ToolchainConstraint("python", command=self.selector["python"]),
                {},
            )
            self.assertNotEqual(tool.command, self.selector["python"])
            tool.require_unchanged()
            shadow = self.shadow / Path(sys.executable).name
            shadow.write_bytes(b"must never execute this selector")
            shadow.chmod(0o755)
            with self.assertRaises(ActionWireError):
                tool.require_unchanged()

    def test_closed_wire_binds_selection_inventory_profile_and_full_request(self):
        observed = self.observed()
        captured = {}

        def transport(*args, **kwargs):
            captured["request"] = args[2]
            captured["response"] = run_command_observation(*args, **kwargs)
            return captured["response"]

        with patch(
            "literate_ai.adapters.action_tool_selectors.run_command_observation",
            side_effect=transport,
        ):
            self.probe(observed)
        request = json.loads(captured["request"])
        response = json.loads(captured["response"])
        receiver = observed.capability.receiver_identity
        changed = dict(
            response,
            selected=[
                {"role": "python", "tool_identity": canonical_identity("wrong").uri}
            ],
        )
        for content in (
            canonical_json_bytes(changed),
            canonical_json_bytes(response | {"extra": True}),
            captured["response"] + b" ",
            b"x" * (MAX_CAPABILITY_BYTES + 1),
            captured["response"].replace(
                b'"schema":', b'"duplicate":1,"duplicate":2,"schema":', 1
            ),
        ):
            with self.assertRaises(ActionWireError):
                decode_selector_response(
                    content, captured["request"], receiver, observed
                )
        altered = request | {
            "selectors": [{"role": "python", "command": ["another-command"]}]
        }
        with self.assertRaises(ActionWireError):
            decode_selector_response(
                captured["response"], canonical_json_bytes(altered), receiver, observed
            )
        stale = replace(
            observed,
            capability=replace(
                observed.capability,
                observed_at=observed.capability.observed_at - timedelta(minutes=6),
            ),
        )
        with self.assertRaises(ActionWireError):
            decode_selector_response(
                captured["response"], captured["request"], receiver, stale
            )
        wrong_profile = replace(
            observed,
            capability=replace(
                observed.capability, build_profile=canonical_identity("changed")
            ),
        )
        with self.assertRaises(ActionWireError):
            decode_selector_response(
                captured["response"], captured["request"], receiver, wrong_profile
            )
        for selectors in (
            [],
            request["selectors"] * 2,
            [{"role": "python", "command": [""]}],
        ):
            with self.assertRaises(ActionWireError):
                decode_selector_request(
                    canonical_json_bytes(request | {"selectors": selectors})
                )
