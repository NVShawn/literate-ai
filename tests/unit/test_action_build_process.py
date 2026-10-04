"""Real BUILD child supervision refuses expired authority and escaped execution."""

import io
import json
import os
import shutil
import sys
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.action_worker import main as receiver_main
from literate_ai.adapters.action_build_process import run_build_worker_process
from literate_ai.adapters.action_build_result import BuildWorkerResult
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    decode_action_response,
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_identity
from tests.support import fixtures_test_action_build_source as source_fixture
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server
from tests.support.fixtures_test_action_build_action import build_request
from tests.support.fixtures_test_action_build_record import build_worker_input
from tests.support.fixtures_test_standard_local_command_adapter import _identity
from tests.support.fixtures_test_standard_project_factory import (
    _command_contracts,
    _toolchain_closure,
)


class ActionBuildProcessTests(unittest.TestCase):
    def setUp(self):
        fixture = source_fixture.ActionBuildSourceTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.root = fixture.root
        self.inputs = fixture.inputs
        self.plan = fixture.plan
        self.input_value = build_worker_input(fixture)
        self.record = self.input_value.to_bytes()
        self.deadline = ActionDispatchDeadline(
            datetime.now(UTC) + timedelta(seconds=30)
        )

    def run_child(self, code, **changes):
        inputs = changes.pop("inputs", self.inputs)
        plan = changes.pop("plan", self.plan)
        record = replace(self.input_value, inputs=inputs, plan=plan).to_bytes()
        arguments = dict(
            launcher=LocalComponentToolBinding(sys.executable, ("-c", code)),
            input_record=record,
            input_identity=record_identity(record),
            deadline=self.deadline,
            cwd=self.root,
            environment=dict(os.environ),
        )
        arguments.update(changes)
        return run_build_worker_process(**arguments)

    def test_exact_input_and_worker_environment_reach_child(self):
        environment = dict(os.environ, LITAI_BUILD_FIXTURE="private-value")
        output = self.run_child(
            "import os,sys; "
            'assert os.environ["LITAI_BUILD_FIXTURE"]=="private-value"; '
            "sys.stdout.buffer.write(sys.stdin.buffer.read())",
            environment=environment,
        )
        self.assertEqual(output, self.record)
        with self.assertRaises(ActionWireError) as raised:
            self.run_child(
                'from pathlib import Path; Path("launched").touch()',
                input_identity=_identity("wrong"),
            )
        self.assertEqual(raised.exception.code, "action_build.input_invalid")
        self.assertFalse((self.root / "launched").exists())
        oversized = b"x" * (17 * 1024 * 1024)
        with self.assertRaises(ActionWireError) as raised:
            self.run_child(
                'from pathlib import Path; Path("launched").touch()',
                input_record=oversized,
                input_identity=record_identity(oversized),
            )
        self.assertEqual(raised.exception.code, "action_build.input_invalid")
        self.assertFalse((self.root / "launched").exists())

        substituted = replace(
            self.input_value, generation_plan_identity=_identity("other-generation")
        ).to_bytes()
        with self.assertRaises(ActionWireError):
            self.run_child(
                'from pathlib import Path; Path("launched").touch()',
                input_record=substituted,
                input_identity=record_identity(substituted),
            )
        self.assertFalse((self.root / "launched").exists())

    def test_supervisor_replaces_reserved_environment_case_variants(self):
        code = (
            "import os,json,sys; "
            "sys.stdin.buffer.read(); "
            "print(json.dumps({k:v for k,v in os.environ.items() "
            "if k.casefold() in ('litai_build_input_identity',"
            "'litai_build_deadline','litai_build_cas','litai_build_workspace')}))"
        )
        launcher = LocalComponentToolBinding(
            sys.executable,
            ("-c", code),
            environment=(
                ("litai_build_input_identity", "stale launcher"),
                ("LITAI_BUILD_DEADLINE", "stale launcher"),
            ),
        )
        output = self.run_child(
            code,
            launcher=launcher,
            environment=dict(
                os.environ,
                LITAI_BUILD_INPUT_IDENTITY="stale caller",
                litai_build_deadline="stale caller",
                litai_build_cas="stale caller",
                LITAI_BUILD_WORKSPACE="stale caller",
            ),
            cas_root=self.fixture.cas.root,
            workspace_root=self.fixture.workspace,
        )
        self.assertEqual(
            json.loads(output),
            {
                "LITAI_BUILD_INPUT_IDENTITY": record_identity(self.record).uri,
                "LITAI_BUILD_DEADLINE": self.deadline.expires_at.isoformat(),
                "LITAI_BUILD_CAS": str(self.fixture.cas.root),
                "LITAI_BUILD_WORKSPACE": str(self.fixture.workspace),
            },
        )

    def test_supervised_child_readmits_and_builds_from_worker_cas(self):
        # The basic source fixture uses placeholder runtime/build-system IDs.
        # Full production composition requires actual observed identities.
        ports = self.fixture.ports
        binding = next(iter(ports.tool_bindings.values()))
        contract = replace(
            self.inputs.contract,
            build_system_toolchain_identity=binding.toolchain_identity,
            language_runtime_identity=binding.toolchain_identity,
        )
        ports.contracts[contract.component_revision.uri] = contract
        intent = ports.create(
            self.input_value.execution_plan,
            self.input_value.generation_plan,
            self.fixture.candidate,
            (),
            (),
        )
        authorization = ports.authorize(
            intent,
            ports.index(
                self.fixture.candidate.component_revision,
                self.fixture.candidate.tree_identity,
            ),
        )
        self.inputs = ports.plan_finalization_inputs(intent, authorization)
        self.plan = ports.finalize(intent, authorization)
        self.input_value = replace(self.input_value, inputs=self.inputs, plan=self.plan)
        self.record = self.input_value.to_bytes()
        shutil.rmtree(self.root / "controller")
        self.deadline = self.fixture.deadline
        environment = dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
            BUILD_RECORD_IDENTITY=record_identity(self.record).uri,
            BUILD_DEADLINE=self.deadline.expires_at.isoformat(),
        )
        contracts, bindings = _command_contracts(self.input_value.execution_plan)
        contracts = tuple(
            self.inputs.contract
            if contract.component_revision == self.plan.component_revision
            else contract
            for contract in contracts
        )
        closure = _toolchain_closure(
            self.input_value.execution_plan, contracts, bindings
        )
        environment["BUILD_WORKER_CONTRACTS"] = json.dumps(
            [item.to_dict() for item in contracts]
        )
        environment["BUILD_PROVIDER_ENVIRONMENT"] = json.dumps(
            dict(closure.provider_environment)
        )
        code = """
import json, os, shutil, sys
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from literate_ai.build_worker import main
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.standard_project import (
    assemble_standard_lifecycle_ports, project_standard_toolchain_closure,
)
from literate_ai.contracts import (
    ComponentCommandPhase, ComponentCommandContract,
    ContentIdentity, canonical_identity,
)
from literate_ai.storage import FileSystemCAS

@contextmanager
def runtime_factory(admitted, registry, recorder):
    print("fixture worker runtime starting")
    binding = LocalComponentToolBinding(sys.executable)
    custody = registry.evidence(admitted.candidate.tree_identity)
    root_ref = custody.managed_graph.root_ref
    closure = project_standard_toolchain_closure(
        admitted.execution_plan,
        contracts=tuple(ComponentCommandContract.from_dict(item)
                        for item in json.loads(os.environ["BUILD_WORKER_CONTRACTS"])),
        provider_environment={key: tuple(value) for key, value in
            json.loads(os.environ["BUILD_PROVIDER_ENVIRONMENT"]).items()},
        tool_bindings=(binding,),
        dependency_observation=HostDependencyObservation(
            ({"type":"application", "name":"fixture-python",
              "version":sys.version.split()[0],
              "properties":[
                  {"name":"literate-ai:dependency-kind", "value":"toolchain"},
                  {"name":"literate-ai:dependency-scope", "value":"build"},
              ],
              "bom-ref":binding.toolchain_identity.uri},),
            ((root_ref, binding.toolchain_identity.uri),),
        ),
        observer_identity=canonical_identity({"observer":"fixture-worker"}),
    )
    composition = assemble_standard_lifecycle_ports(
        source_trees=registry, object_root=Path("objects").resolve(),
        toolchain_closure=closure, command_phases=(ComponentCommandPhase.BUILD,),
    )
    composition.ports.retain_evidence_with(recorder)
    try:
        yield composition.ports
    finally:
        shutil.rmtree(composition.ports.object_root)

raise SystemExit(main(runtime_factory=runtime_factory))
"""

        def checked_process(*args, **kwargs):
            completed = run_bounded_process(*args, **kwargs)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed

        runtime = discover_python_toolchain(pinned_command=(sys.executable,))
        launcher = LocalComponentToolBinding(
            sys.executable,
            ("-c", code),
            authority_identity=canonical_identity(
                {"runtime": runtime.identity, "code": code}
            ),
            _authority_guard=runtime.require_unchanged,
        )
        configured = ConfiguredBuildWorker(
            launcher, (binding,), environment=environment
        )
        request, records = build_request(self.input_value, self.deadline)
        wire = encode_action_request(request, self.deadline, records)
        stdout = io.BytesIO()
        blobs = {
            blob_path(item.blob): self.fixture.controller_cas.path_for(item.blob)
            for item in self.fixture.files
        }
        with (
            source_cas_server(blobs) as (endpoint, fetched),
            patch(
                "literate_ai.action_worker.sys.stdin",
                SimpleNamespace(buffer=io.BytesIO(wire)),
            ),
            patch(
                "literate_ai.action_worker.sys.stdout", SimpleNamespace(buffer=stdout)
            ),
            patch.dict(
                os.environ,
                {"LITAI_ACTION_WORKER_IDENTITY": request.worker.worker_identity.uri},
            ),
            patch(
                "literate_ai.adapters.action_worker_process.run_bounded_process",
                side_effect=checked_process,
            ),
        ):
            status = receiver_main(
                [
                    "--cas",
                    str(self.fixture.cas.root),
                    "--workspace",
                    str(self.fixture.workspace),
                    "--source-cas-url",
                    endpoint,
                    "--allow-http",
                ],
                build_worker=configured,
            )
        self.assertEqual(status, 0)
        self.assertTrue(fetched)
        outcome, result = decode_action_response(stdout.getvalue(), request)
        self.assertIsNone(outcome.failure_code)
        admitted_result = BuildWorkerResult.admit(
            result,
            record_identity(result),
            input_record=self.record,
            input_identity=record_identity(self.record),
            deadline=self.deadline,
        )
        files = read_directory_export(
            self.fixture.cas.get_bytes(admitted_result.artifact_archive),
            admitted_result.artifact_archive,
            max_bytes=256 * 1024 * 1024,
            max_entries=65534,
        )
        self.assertTrue(
            any(item.content.splitlines() == [b"known-output"] for item in files)
        )
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])
        self.assertFalse((self.root / "objects").exists())

    def test_preexisting_refusal_never_launches_child(self):
        code = 'from pathlib import Path; Path("launched").touch()'
        revoked = replace(
            self.inputs.authorization,
            grant=replace(self.inputs.authorization.grant, revoked=True),
        )
        for changes, expected in (
            (
                {"inputs": replace(self.inputs, authorization=revoked)},
                "action_build.input_invalid",
            ),
            ({"cancelled": lambda: True}, "action_build.cancelled"),
            (
                {
                    "deadline": ActionDispatchDeadline(
                        datetime.now(UTC) - timedelta(seconds=1)
                    )
                },
                "action_wire.expired",
            ),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaises(ActionWireError) as raised,
            ):
                self.run_child(code, **changes)
            self.assertEqual(raised.exception.code, expected)
        self.assertFalse((self.root / "launched").exists())

        worker_file = self.root / "worker"
        worker_file.write_bytes(b"original-launcher")
        launcher = LocalComponentToolBinding(str(worker_file))
        worker_file.write_bytes(b"changed-launcher")
        with self.assertRaises(ActionWireError) as raised:
            self.run_child(code, launcher=launcher)
        self.assertEqual(raised.exception.code, "action_tools.changed")
        self.assertFalse((self.root / "launched").exists())

    def test_expiry_and_cancellation_interrupt_a_running_child(self):
        code = (
            'from pathlib import Path; import time; Path("ready").touch(); '
            'time.sleep(30); Path("late").touch()'
        )
        for mode in ("expiry", "cancel"):
            with self.subTest(mode=mode):
                ready = self.root / "ready"
                ready.unlink(missing_ok=True)
                changes = (
                    {
                        "deadline": ActionDispatchDeadline(
                            self.inputs.authorization.grant.expires_at
                            + timedelta(minutes=1)
                        ),
                        "clock": lambda ready=ready: (
                            self.inputs.authorization.grant.expires_at
                            if ready.exists()
                            else datetime.now(UTC)
                        ),
                    }
                    if mode == "expiry"
                    else {"cancelled": ready.exists}
                )
                started = time.monotonic()
                with self.assertRaises(ActionWireError) as raised:
                    self.run_child(code, **changes)
                self.assertEqual(
                    raised.exception.code,
                    "action_build.authority_invalid"
                    if mode == "expiry"
                    else "action_build.cancelled",
                )
                self.assertTrue(ready.exists())
                self.assertLess(time.monotonic() - started, 10)
                self.assertFalse((self.root / "late").exists())

    def test_overflow_and_nonzero_exit_hide_private_child_diagnostics(self):
        for code, expected in (
            (
                'import sys; sys.stderr.write("private-value"); sys.exit(7)',
                "action_build.process_failed",
            ),
            (
                'import sys; sys.stdout.buffer.write(b"x"*(17*1024*1024))',
                "action_build.output_oversized",
            ),
            (
                'import sys; sys.stderr.buffer.write(b"x"*(65*1024))',
                "action_build.output_oversized",
            ),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaises(ActionWireError) as raised,
            ):
                self.run_child(code)
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn("private-value", str(raised.exception))

    def test_successful_parent_cannot_leave_detached_output_writes(self):
        child = (
            "from pathlib import Path; import time; "
            'Path("descendant-ready").touch(); time.sleep(1); Path("escaped").touch()'
        )
        code = (
            "import subprocess,sys,time\nfrom pathlib import Path\n"
            f'subprocess.Popen([sys.executable,"-c",{child!r}], '
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            'while not Path("descendant-ready").exists(): time.sleep(.01)\n'
            'sys.stdin.buffer.read(); print("complete")\n'
        )
        self.assertEqual(self.run_child(code).strip(), b"complete")
        time.sleep(1.2)
        self.assertFalse((self.root / "escaped").exists())

    def test_elapsed_action_deadline_stops_real_child(self):
        started = time.monotonic()
        with self.assertRaises(ActionWireError) as raised:
            self.run_child(
                "from pathlib import Path; import time; "
                'Path("ready").touch(); time.sleep(30); Path("late").touch()',
                deadline=ActionDispatchDeadline(
                    datetime.now(UTC) + timedelta(seconds=2)
                ),
            )
        self.assertIn(
            raised.exception.code, {"action_build.expired", "action_wire.expired"}
        )
        self.assertTrue((self.root / "ready").exists())
        self.assertFalse((self.root / "late").exists())
        self.assertLess(time.monotonic() - started, 15)

    def test_short_grant_stops_child_before_later_action_deadline(self):
        authorization = replace(
            self.inputs.authorization,
            grant=replace(
                self.inputs.authorization.grant,
                expires_at=datetime.now(UTC) + timedelta(seconds=2),
            ),
        )
        inputs = replace(self.inputs, authorization=authorization)
        started = time.monotonic()
        with self.assertRaises(ActionWireError) as raised:
            self.run_child(
                "from pathlib import Path; import time; "
                'Path("ready").touch(); time.sleep(30); Path("late").touch()',
                inputs=inputs,
                plan=inputs.finalize(),
            )
        self.assertIn(
            raised.exception.code,
            {"action_build.expired", "action_build.authority_invalid"},
        )
        self.assertTrue((self.root / "ready").exists())
        self.assertFalse((self.root / "late").exists())
        self.assertLess(time.monotonic() - started, 15)
