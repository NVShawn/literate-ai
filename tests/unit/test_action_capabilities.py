"""Explicit protocol opt-in and real bounded command capability probes."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from literate_ai.adapters.action_capabilities import (
    MAX_CAPABILITY_BYTES,
    decode_capability_request,
    decode_capability_response,
    encode_capability_response,
    probe_command_action_capabilities,
    receiver_code_identity,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import ContractValidationError
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.unit import test_action_source_index as source_fixture
from tests.unit.action_deadline import ACTION_TEST_DEADLINE
from tests.unit.test_schema_catalog import SchemaCatalog


class ActionCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceIndexActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.worker = replace(
            self.fixture.worker, action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL
        )
        # These scenarios perform multiple probes and sometimes an index dispatch.
        # Each production probe retains its own cap inside this scenario budget.
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        self.fixture.deadline = self.deadline
        self.fixture.request = replace(
            self.fixture.request, deadline_identity=self.deadline.identity
        )

    def probe(self, worker=None, **kwargs):
        worker = self.worker if worker is None else worker
        return probe_command_action_capabilities(
            worker,
            self.deadline,
            cwd=self.fixture.root,
            environment={**os.environ, "INDEX_WORKER_IDENTITY": worker.identity.uri},
            **kwargs,
        )

    def request(self, nonce="a" * 32):
        return canonical_json_bytes(
            {
                "schema": "literate-ai/action-capability-request@1",
                "worker_identity": self.worker.identity.uri,
                "nonce": nonce,
                "deadline": self.deadline.to_dict(),
            }
        )

    def test_protocol_opt_in_preserves_legacy_document_and_schema(self):
        legacy = self.fixture.worker.to_dict()
        self.assertNotIn("action_protocol", legacy)
        self.assertEqual(
            ExecutionWorker.from_dict(legacy).identity, self.fixture.worker.identity
        )
        self.assertNotEqual(self.worker.identity, self.fixture.worker.identity)
        self.assertEqual(ExecutionWorker.from_dict(self.worker.to_dict()), self.worker)
        catalog = SchemaCatalog()
        catalog.validate(self.worker.SCHEMA, self.worker.to_dict())
        catalog.validate(self.fixture.worker.SCHEMA, legacy)
        for protocol in ("unknown", "", True):
            with (
                self.subTest(protocol=protocol),
                self.assertRaises(ContractValidationError),
            ):
                replace(self.worker, action_protocol=protocol)
        with self.assertRaises(ContractValidationError):
            ExecutionWorker(
                "local",
                ExecutionWorkerKind.LOCAL,
                action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
            )

    def test_legacy_command_is_never_invoked_as_a_capability_probe(self):
        with patch(
            "literate_ai.adapters.action_capabilities.run_bounded_process"
        ) as run:
            with self.assertRaises(ActionWireError) as raised:
                self.probe(self.fixture.worker)
            self.assertEqual(raised.exception.code, "action_capability.not_declared")
            run.assert_not_called()

    def test_probe_caps_its_process_budget_even_with_a_long_action_deadline(self):
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        with patch(
            "literate_ai.adapters.action_capabilities.run_bounded_process",
            wraps=run_bounded_process,
        ) as run:
            self.assertEqual(
                self.probe().actions,
                (
                    LifecycleActionKind.AUTHORIZE,
                    LifecycleActionKind.BUILD_INTENT,
                    LifecycleActionKind.INDEX,
                    LifecycleActionKind.PLAN,
                ),
            )
        self.assertLessEqual(run.call_args.kwargs["timeout_seconds"], 60)
        self.assertGreater(run.call_args.kwargs["timeout_seconds"], 0)

    def test_short_action_deadline_stops_a_slow_receiver(self):
        worker = replace(
            self.worker,
            command=(sys.executable, "-c", "import time; time.sleep(6)"),
        )
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=1))
        started = time.monotonic()
        with (
            patch(
                "literate_ai.adapters.action_capabilities.receiver_code_identity",
                return_value=canonical_identity("controller code"),
            ),
            self.assertRaises(ActionWireError) as caught,
        ):
            self.probe(worker)
        self.assertIn(
            caught.exception.code,
            ("action_wire.expired", "action_capability.probe_failed"),
        )
        self.assertLess(time.monotonic() - started, 5)

    def test_real_probe_binds_runtime_worker_and_read_only_capabilities(self):
        first = self.probe()
        second = self.probe()
        self.assertEqual(first.worker_identity, self.worker.identity)
        self.assertEqual(first.receiver_identity, receiver_code_identity())
        self.assertEqual(
            first.actions,
            (
                LifecycleActionKind.AUTHORIZE,
                LifecycleActionKind.BUILD_INTENT,
                LifecycleActionKind.INDEX,
                LifecycleActionKind.PLAN,
            ),
        )
        self.assertEqual(first.source_handoff, ("filesystem-cas",))
        self.assertEqual(first.capability_identity, second.capability_identity)
        self.assertNotEqual(first.request_identity, second.request_identity)
        self.assertNotEqual(first.identity, second.identity)
        first.require_current(self.worker, receiver_code_identity())
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_request_file_mode_supports_probe_and_actual_index_dispatch(self):
        worker = replace(
            self.worker,
            command=(*self.worker.command, "--request-file", "{request_file}"),
        )
        self.assertEqual(self.probe(worker).worker_identity, worker.identity)
        self.fixture.worker = worker
        self.fixture.catalog = ExecutionWorkerCatalog((worker,))
        self.fixture.request = replace(
            self.fixture.request,
            worker=replace(
                self.fixture.request.worker,
                worker_identity=worker.identity,
                catalog_identity=self.fixture.catalog.identity,
            ),
        )
        self.assertIsNone(self.fixture.dispatch().failure_code)

    def test_http_capability_requires_valid_private_configuration_without_fetching(
        self,
    ):
        configured = replace(
            self.worker,
            command=(
                *self.worker.command,
                "--source-cas-url",
                "https://example.invalid/cas",
            ),
        )
        observed = self.probe(configured)
        self.assertEqual(observed.source_handoff, ("filesystem-cas", "http-cas"))
        unresolved = replace(
            configured,
            command=(
                *configured.command,
                "--source-token-env",
                "UNBOUND_SOURCE_TOKEN",
            ),
        )
        with self.assertRaises(ActionWireError) as raised:
            self.probe(unresolved)
        self.assertEqual(raised.exception.code, "action_capability.probe_failed")

    def test_replay_changed_worker_code_and_unsupported_capabilities_refuse(self):
        content = self.request()
        request, deadline = decode_capability_request(content)
        response = encode_capability_response(
            request, deadline, self.worker.identity, http_source=False
        )
        expected = receiver_code_identity()
        with self.assertRaises(ActionWireError):
            decode_capability_response(response, self.request("b" * 32), expected)
        changes = (
            {"worker_identity": canonical_identity("other-worker").uri},
            {"receiver_identity": canonical_identity("other-code").uri},
            {"actions": ["build"]},
            {"python_version": [True, 14, 0]},
            {"source_handoff": ["unknown"]},
            {"unexpected": "field"},
        )
        for change in changes:
            with self.subTest(change=change):
                document = json.loads(response)
                document.update(change)
                with self.assertRaises(ActionWireError):
                    decode_capability_response(
                        canonical_json_bytes(document), content, expected
                    )

    def test_build_inventory_is_bounded_canonical_and_bound_into_facts(self):
        content = self.request()
        request, deadline = decode_capability_request(content)
        tools = tuple(
            sorted((canonical_identity(i) for i in range(256)), key=lambda i: i.uri)
        )
        response = encode_capability_response(
            request,
            deadline,
            self.worker.identity,
            http_source=False,
            build_profile=canonical_identity("configured"),
            build_toolchains=tools,
        )
        self.assertLess(len(response), MAX_CAPABILITY_BYTES)
        expected = receiver_code_identity()
        observed = decode_capability_response(response, content, expected)
        self.assertEqual(observed.build_toolchains, tools)
        self.assertIn(LifecycleActionKind.BUILD, observed.actions)
        self.assertNotEqual(
            observed.capability_identity,
            replace(
                observed, build_profile=canonical_identity("changed")
            ).capability_identity,
        )
        self.assertNotEqual(
            observed.capability_identity,
            replace(observed, build_toolchains=tools[:-1]).capability_identity,
        )
        original = json.loads(response)
        changes = (
            {"build": None},
            {"build": {}},
            {"build": {**original["build"], "unknown": True}},
            {"build": {**original["build"], "toolchain_identities": []}},
            {
                "build": {
                    **original["build"],
                    "toolchain_identities": [tools[0].uri] * 2,
                }
            },
            {
                "build": {
                    **original["build"],
                    "toolchain_identities": [i.uri for i in reversed(tools)],
                }
            },
            {
                "build": {
                    **original["build"],
                    "toolchain_identities": [i.uri for i in tools] + [tools[0].uri],
                }
            },
            {"actions": ["index"]},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ActionWireError):
                decode_capability_response(
                    canonical_json_bytes({**original, **change}), content, expected
                )

    def test_test_inventory_is_bounded_canonical_and_bound_into_facts(self):
        content = self.request()
        request, deadline = decode_capability_request(content)
        tools = tuple(
            sorted((canonical_identity(i) for i in range(256)), key=lambda i: i.uri)
        )
        response = encode_capability_response(
            request,
            deadline,
            self.worker.identity,
            http_source=False,
            test_profile=canonical_identity("configured"),
            test_toolchains=tools,
        )
        self.assertLess(len(response), MAX_CAPABILITY_BYTES)
        expected = receiver_code_identity()
        observed = decode_capability_response(response, content, expected)
        self.assertEqual(observed.test_toolchains, tools)
        self.assertIn(LifecycleActionKind.TEST, observed.actions)
        self.assertNotEqual(
            observed.capability_identity,
            replace(
                observed, test_profile=canonical_identity("changed")
            ).capability_identity,
        )
        self.assertNotEqual(
            observed.capability_identity,
            replace(observed, test_toolchains=tools[:-1]).capability_identity,
        )
        original = json.loads(response)
        changes = (
            {"test": None},
            {"test": {}},
            {"test": {**original["test"], "unknown": True}},
            {"test": {**original["test"], "toolchain_identities": []}},
            {
                "test": {
                    **original["test"],
                    "toolchain_identities": [tools[0].uri] * 2,
                }
            },
            {
                "test": {
                    **original["test"],
                    "toolchain_identities": [i.uri for i in reversed(tools)],
                }
            },
            {
                "test": {
                    **original["test"],
                    "toolchain_identities": [i.uri for i in tools] + [tools[0].uri],
                }
            },
            {"actions": ["index"]},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ActionWireError):
                decode_capability_response(
                    canonical_json_bytes({**original, **change}), content, expected
                )

    def test_execute_inventory_is_bounded_canonical_and_bound_into_facts(self):
        content = self.request()
        request, deadline = decode_capability_request(content)
        tools = tuple(
            sorted((canonical_identity(i) for i in range(256)), key=lambda i: i.uri)
        )
        response = encode_capability_response(
            request,
            deadline,
            self.worker.identity,
            http_source=False,
            execute_profile=canonical_identity("configured"),
            execute_toolchains=tools,
        )
        self.assertLess(len(response), MAX_CAPABILITY_BYTES)
        expected = receiver_code_identity()
        observed = decode_capability_response(response, content, expected)
        self.assertEqual(observed.execute_toolchains, tools)
        self.assertIn(LifecycleActionKind.EXECUTE, observed.actions)
        self.assertNotEqual(
            observed.capability_identity,
            replace(
                observed, execute_profile=canonical_identity("changed")
            ).capability_identity,
        )
        self.assertNotEqual(
            observed.capability_identity,
            replace(observed, execute_toolchains=tools[:-1]).capability_identity,
        )
        original = json.loads(response)
        changes = (
            {"execute": None},
            {"execute": {}},
            {"execute": {**original["execute"], "unknown": True}},
            {"execute": {**original["execute"], "toolchain_identities": []}},
            {
                "execute": {
                    **original["execute"],
                    "toolchain_identities": [tools[0].uri] * 2,
                }
            },
            {
                "execute": {
                    **original["execute"],
                    "toolchain_identities": [i.uri for i in reversed(tools)],
                }
            },
            {
                "execute": {
                    **original["execute"],
                    "toolchain_identities": [i.uri for i in tools] + [tools[0].uri],
                }
            },
            {"actions": ["index"]},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ActionWireError):
                decode_capability_response(
                    canonical_json_bytes({**original, **change}), content, expected
                )

    def test_real_configured_receiver_advertises_observed_build_tools(self):
        code = """
import sys
from literate_ai.action_worker import main
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
binding = LocalComponentToolBinding(sys.executable, ('-c', 'raise SystemExit(2)'))
configured = ConfiguredBuildWorker(binding, (binding,), environment=dict())
raise SystemExit(main(build_worker=configured))
"""
        worker = replace(
            self.worker,
            command=(sys.executable, "-I", "-c", code, *self.worker.command[4:]),
        )
        observed = self.probe(worker)
        self.assertIn(LifecycleActionKind.BUILD, observed.actions)
        self.assertIsNotNone(observed.build_profile)
        self.assertEqual(len(observed.build_toolchains), 1)
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_real_configured_receiver_advertises_observed_test_tools(self):
        code = """
import sys
from literate_ai.action_worker import main
from literate_ai.adapters.action_test_worker import ConfiguredTestWorker
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
binding = LocalComponentToolBinding(sys.executable, ('-c', 'raise SystemExit(2)'))
configured = ConfiguredTestWorker(binding, (binding,), environment=dict())
raise SystemExit(main(test_worker=configured))
"""
        worker = replace(
            self.worker,
            command=(sys.executable, "-I", "-c", code, *self.worker.command[4:]),
        )
        observed = self.probe(worker)
        self.assertIn(LifecycleActionKind.TEST, observed.actions)
        self.assertNotIn(LifecycleActionKind.BUILD, observed.actions)
        self.assertIsNotNone(observed.test_profile)
        self.assertEqual(len(observed.test_toolchains), 1)
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_real_configured_receiver_advertises_observed_execute_tools(self):
        code = """
import sys
from literate_ai.action_worker import main
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
binding = LocalComponentToolBinding(sys.executable, ('-c', 'raise SystemExit(2)'))
configured = ConfiguredExecuteWorker(binding, (binding,), environment=dict())
raise SystemExit(main(execute_worker=configured))
"""
        worker = replace(
            self.worker,
            command=(sys.executable, "-I", "-c", code, *self.worker.command[4:]),
        )
        observed = self.probe(worker)
        self.assertIn(LifecycleActionKind.EXECUTE, observed.actions)
        self.assertNotIn(LifecycleActionKind.BUILD, observed.actions)
        self.assertIsNotNone(observed.execute_profile)
        self.assertEqual(len(observed.execute_toolchains), 1)
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_combined_phase_profiles_are_independent_and_bound(self):
        content = self.request()
        request, deadline = decode_capability_request(content)
        response = encode_capability_response(
            request,
            deadline,
            self.worker.identity,
            http_source=False,
            build_profile=canonical_identity("build"),
            build_toolchains=(canonical_identity("compiler"),),
            test_profile=canonical_identity("test"),
            test_toolchains=(canonical_identity("runner"),),
            test_standard_tools=canonical_identity("test-inventory"),
            execute_profile=canonical_identity("execute"),
            execute_toolchains=(canonical_identity("runtime"),),
            execute_standard_tools=canonical_identity("execute-inventory"),
        )
        observed = decode_capability_response(
            response, content, receiver_code_identity()
        )
        self.assertIn(LifecycleActionKind.BUILD, observed.actions)
        self.assertIn(LifecycleActionKind.TEST, observed.actions)
        self.assertNotEqual(observed.build_profile, observed.test_profile)
        self.assertIn(LifecycleActionKind.EXECUTE, observed.actions)
        self.assertNotEqual(observed.test_profile, observed.execute_profile)
        self.assertNotEqual(
            observed.capability_identity,
            replace(
                observed, execute_standard_tools=canonical_identity("changed")
            ).capability_identity,
        )
        self.assertNotEqual(
            observed.capability_identity,
            replace(
                observed, test_standard_tools=canonical_identity("changed")
            ).capability_identity,
        )
        document = json.loads(response)
        del document["test"]
        with self.assertRaises(ActionWireError):
            decode_capability_response(
                canonical_json_bytes(document), content, receiver_code_identity()
            )

    def test_stale_future_or_changed_route_observations_refuse(self):
        observed = self.probe()
        expected = receiver_code_identity()
        for timestamp in (
            datetime.now(UTC) - timedelta(minutes=6),
            datetime.now(UTC) + timedelta(minutes=1),
        ):
            with self.subTest(timestamp=timestamp), self.assertRaises(ActionWireError):
                replace(observed, observed_at=timestamp).require_current(
                    self.worker, expected
                )
        with self.assertRaises(ActionWireError):
            observed.require_current(
                replace(self.worker, target_profile="other"), expected
            )

    def test_private_worker_identity_mismatch_refuses_real_probe(self):
        with self.assertRaises(ActionWireError) as raised:
            probe_command_action_capabilities(
                self.worker,
                self.deadline,
                cwd=self.fixture.root,
                environment={
                    **os.environ,
                    "INDEX_WORKER_IDENTITY": canonical_identity("other").uri,
                },
            )
        self.assertEqual(raised.exception.code, "action_capability.probe_failed")

    def test_receiver_code_identity_changes_and_refuses_source_links(self):
        root = self.fixture.root / "package"
        root.mkdir()
        module = root / "module.py"
        module.write_text("VALUE = 1\n")
        first = receiver_code_identity(package_root=root)
        module.write_text("VALUE = 2\n")
        self.assertNotEqual(first, receiver_code_identity(package_root=root))
        link = root / "linked"
        try:
            link.symlink_to(self.fixture.root, target_is_directory=True)
        except OSError:
            self.skipTest("directory links are unavailable")
        with self.assertRaises(ActionWireError):
            receiver_code_identity(package_root=root)

    def test_accept_profile_without_application_tools_roundtrips_and_binds_facts(self):
        content = self.request()
        request, deadline = decode_capability_request(content)
        profile = canonical_identity("accept-profile")
        response = encode_capability_response(
            request,
            deadline,
            self.worker.identity,
            http_source=False,
            accept_profile=profile,
        )
        expected = receiver_code_identity()
        observed = decode_capability_response(response, content, expected)
        self.assertEqual(observed.accept_profile, profile)
        self.assertIn(LifecycleActionKind.ACCEPT, observed.actions)
        self.assertNotEqual(
            observed.capability_identity,
            replace(
                observed, accept_profile=canonical_identity("changed")
            ).capability_identity,
        )
        self.assertEqual(
            json.loads(response)["accept"], {"profile_identity": profile.uri}
        )
        original = json.loads(response)
        for facts in (
            None,
            {},
            {"profile_identity": profile.uri, "toolchain_identities": []},
            {"profile_identity": "bad"},
        ):
            with self.subTest(facts=facts), self.assertRaises(ActionWireError):
                decode_capability_response(
                    canonical_json_bytes({**original, "accept": facts}),
                    content,
                    expected,
                )
        for changed in (
            {key: value for key, value in original.items() if key != "accept"},
            {
                **original,
                "actions": [item for item in original["actions"] if item != "accept"],
            },
        ):
            with self.assertRaises(ActionWireError):
                decode_capability_response(
                    canonical_json_bytes(changed), content, expected
                )

    def test_unconfigured_accept_is_absent_and_invalid_profile_refuses(self):
        content = self.request()
        request, deadline = decode_capability_request(content)
        response = encode_capability_response(
            request, deadline, self.worker.identity, http_source=False
        )
        self.assertNotIn("accept", json.loads(response))
        with self.assertRaises(ActionWireError):
            encode_capability_response(
                request,
                deadline,
                self.worker.identity,
                http_source=False,
                accept_profile="foreign",
            )
