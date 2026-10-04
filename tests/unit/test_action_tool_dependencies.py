"""Native dependency transfer binds selected tools and admitted worker state."""

import json
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from literate_ai.adapters.action_capabilities import run_command_observation
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_tool_dependencies import (
    MAX_DEPENDENCY_RESPONSE_BYTES,
    decode_dependency_response,
    probe_command_tool_dependencies,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from tests.support import fixtures_test_action_tool_observation as tool_fixture


class ActionToolDependencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tool_fixture.ActionToolObservationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def probe(self, admitted, **kwargs):
        fixture = self.fixture
        return probe_command_tool_dependencies(
            fixture.worker,
            admitted,
            fixture.deadline,
            admitted.build_toolchains,
            root_ref="urn:literate-ai:component:test",
            cwd=fixture.root,
            environment=fixture.environment,
            **kwargs,
        )

    def test_real_stdio_and_request_file_graphs_and_retained_identity(self):
        fixture = self.fixture
        for request_file in (False, True):
            with self.subTest(request_file=request_file):
                if request_file:
                    fixture.worker = replace(
                        fixture.worker,
                        command=(
                            *fixture.worker.command,
                            "--request-file",
                            "{request_file}",
                        ),
                    )
                admitted = fixture.admission()
                self.assertIsNotNone(admitted.accept_profile)
                first = self.probe(admitted)
                self.assertTrue(first.dependencies.observation.components)
                again = self.probe(
                    admitted, expected_identity=first.dependencies.identity
                )
                self.assertEqual(first.dependencies, again.dependencies)
                self.assertEqual(list(fixture.workspace.iterdir()), [])

    def test_changed_profile_wrong_retained_graph_and_unknown_selection_refuse(self):
        fixture = self.fixture
        admitted = fixture.admission()
        with self.assertRaises(ActionWireError):
            self.probe(admitted, expected_identity=canonical_identity("other graph"))
        fixture.environment["BUILD_SETTING"] = "changed"
        with self.assertRaises(ActionWireError):
            self.probe(admitted)
        with patch(
            "literate_ai.adapters.action_tool_dependencies.run_command_observation"
        ) as transport:
            with self.assertRaises(ActionWireError):
                probe_command_tool_dependencies(
                    fixture.worker,
                    admitted,
                    fixture.deadline,
                    (canonical_identity("missing"),),
                    root_ref="root",
                    cwd=fixture.root,
                    environment=fixture.environment,
                )
            transport.assert_not_called()

    def test_response_binds_full_request_capability_selection_and_closed_bounds(self):
        admitted = self.fixture.admission()
        captured = {}

        def transport(*args, **kwargs):
            captured["request"] = args[2]
            captured["response"] = run_command_observation(*args, **kwargs)
            self.assertEqual(
                kwargs["stdout_limit_bytes"], MAX_DEPENDENCY_RESPONSE_BYTES
            )
            return captured["response"]

        with patch(
            "literate_ai.adapters.action_tool_dependencies.run_command_observation",
            side_effect=transport,
        ):
            result = self.probe(admitted)
        document = json.loads(captured["response"])
        request = json.loads(captured["request"])
        expected_receiver = result.capability.receiver_identity
        mutations = [
            document | {"request_identity": canonical_identity("different").uri},
            document | {"extra": True},
            document | {"dependencies": document["dependencies"] | {"root": "other"}},
            document
            | {
                "dependencies": document["dependencies"]
                | {"tools": [canonical_identity("other tool").uri]}
            },
        ]
        for value in mutations:
            with self.subTest(keys=list(value)), self.assertRaises(ActionWireError):
                decode_dependency_response(
                    canonical_json_bytes(value),
                    captured["request"],
                    expected_receiver,
                    admitted,
                )
        for content in (
            captured["response"] + b" ",
            b"x" * (MAX_DEPENDENCY_RESPONSE_BYTES + 1),
            captured["response"].replace(
                b'"schema":', b'"duplicate":1,"duplicate":2,"schema":', 1
            ),
        ):
            with self.assertRaises(ActionWireError):
                decode_dependency_response(
                    content, captured["request"], expected_receiver, admitted
                )
        altered = request | {"capability": request["capability"] | {"nonce": "0" * 32}}
        with self.assertRaises(ActionWireError):
            decode_dependency_response(
                captured["response"],
                canonical_json_bytes(altered),
                expected_receiver,
                admitted,
            )
        with self.assertRaises(ActionWireError):
            decode_dependency_response(
                captured["response"],
                captured["request"],
                expected_receiver,
                replace(
                    admitted, observed_at=admitted.observed_at - timedelta(minutes=6)
                ),
            )
