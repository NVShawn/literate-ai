"""Native receiving/admission; producer evidence is an explicit contract fixture."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_cargo_execution_v2 as executor
from literate_ai.adapters.retained_cargo_admission_v2 import (
    admit_retained_cargo_consumer_v2,
    read_retained_cargo_retirement_authority_v2,
)
from literate_ai.adapters.retained_cargo_execution import RetainedCargoExecutionError
from literate_ai.adapters.retained_cargo_execution_inputs_v2 import (
    read_retained_cargo_execution_inputs_v2,
)
from literate_ai.adapters.retained_cargo_execution_v2 import (
    execute_retained_cargo_consumer_v2,
)
from literate_ai.adapters.retained_cargo_files_v2 import read_retained_cargo_files_v2
from literate_ai.adapters.retained_cargo_import_v2 import RetainedCargoCurrentInputsV2
from literate_ai.adapters.retained_cargo_materialization_v2 import (
    materialize_retained_cargo_archive_v2,
)
from literate_ai.adapters.retained_cargo_scratch_v2 import (
    reserve_retained_cargo_scratch_v2,
)
from literate_ai.adapters.retained_cargo_wrapper_v2 import (
    retained_cargo_wrapper_context_v2,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.retained_cargo_admission import RetainedCargoSourceRetirement
from literate_ai.contracts.retained_cargo_native_custody import (
    RetainedCargoNativeCustodyPolicy,
    RetainedCargoOutputBudget,
)
from literate_ai.contracts.retained_cargo_test_protocol import (
    RetainedCargoLibtestProtocol,
)
from tests.support import fixtures_retained_cargo_native_v2 as native_fixtures
from tests.support.fixtures_retained_cargo_scratch import scratch_review_fixture
from tests.support.fixtures_test_retained_cargo_import import blob


@unittest.skipUnless(
    os.name != "nt" and all(shutil.which(x) for x in ("cargo", "rustc", "make", "git")),
    "requires native POSIX Cargo/Rust and retained Make/Git adoption",
)
class RetainedCargoNativeReceivingV2Tests(unittest.TestCase):
    def open_case(self, *, retire=True, native_custody=None):
        base = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="nrv2-"))
        ).resolve()
        fixture = native_fixtures.NativeReceivingFixture(base)
        self.reviewed, self.fixture = self.enterContext(
            scratch_review_fixture(
                base,
                with_provider=True,
                fixture=fixture,
                native_custody=native_custody,
            )
        )
        root = self.reviewed.project.root
        # app.py was present in _legacy_project BEFORE adoption and is in the
        # original conversion inventory. Exercise its actual relocated path,
        # rather than a post-conversion replacement or an invented absent root.
        self.original_path = root / self.reviewed.plan.common.workspace_root / "app.py"
        self.original_bytes = self.original_path.read_bytes()
        self.assertEqual(self.original_bytes, b"print('retained')\n")
        if retire:
            self.original_path.unlink()
        retirement = RetainedCargoSourceRetirement(
            self.reviewed.project.definition.project_id,
            self.reviewed.binding.identity,
            (self.original_path.relative_to(root).as_posix(),),
        )
        self.retirement_bytes = canonical_json_bytes(retirement.to_dict())
        (root / "review/retirement.json").write_bytes(self.retirement_bytes)
        current = RetainedCargoCurrentInputsV2(
            self.reviewed,
            replace(
                fixture.inputs,
                importer_project_id=self.reviewed.project.definition.project_id,
                reviewed_binding_identity=self.reviewed.binding.identity,
                configured_store_id=self.reviewed.binding.common.source_store_id,
            ),
        )
        context = self.enterContext(retained_cargo_wrapper_context_v2(self.reviewed))
        scratch = self.enterContext(reserve_retained_cargo_scratch_v2(context))
        files = read_retained_cargo_files_v2(
            self.reviewed, manifest_state="after", package_state="absent"
        )
        self.materialized = materialize_retained_cargo_archive_v2(
            files,
            scratch=scratch,
            **{**fixture.calls, "read_current": lambda: current},
        )
        self.inputs = read_retained_cargo_execution_inputs_v2(
            self.materialized, environment=fixture.environment
        )

    def retirement(self):
        return read_retained_cargo_retirement_authority_v2(
            self.materialized,
            retirement_path="review/retirement.json",
            reviewed_retirement=blob(self.retirement_bytes),
        )

    def execution(self):
        return {
            "cargo": self.fixture.cargo,
            "rustc": self.fixture.rustc,
            "gate_tools": {"cargo": self.fixture.cargo},
            "environment": self.fixture.environment,
            "consumer_inputs": self.inputs,
            "allow_host_execution": True,
            "read_exclusion_permission": self.fixture.read_permission,
            # Complete native input/runtime observations need the API's finite
            # default allowance on two-CPU workers; retain all deadline guards.
            "timeout_seconds": 900,
        }

    def test_real_patch_workspace_gates_protocols_and_retired_admission(self):
        case_start = time.monotonic()
        measured = {"full_input_checks": 0, "commands": []}
        telemetry_rows = 0

        def emit(event, **values):
            nonlocal telemetry_rows
            if telemetry_rows >= 256:
                return
            telemetry_rows += 1
            print(
                "native receiving progress "
                + json.dumps(
                    {
                        "event": event,
                        "elapsed_seconds": time.monotonic() - case_start,
                        "full_input_checks": measured["full_input_checks"],
                        **values,
                    }
                ),
                file=sys.stderr,
                flush=True,
            )

        emit("fixture-start")
        self.open_case()
        # Expected coverage comes from independently reviewed declarations,
        # never from materialized package counts or observed outcomes.
        expected_targets = self.reviewed.inventory.targets
        expected_libtests = tuple(
            target
            for target in expected_targets
            if isinstance(target.protocol, RetainedCargoLibtestProtocol)
        )
        emit(
            "fixture-ready",
            reviewed_targets=len(expected_targets),
            reviewed_libtests=len(expected_libtests),
        )
        authority = self.retirement()
        emit("retirement-ready")
        actual_run = executor.run_bounded_process
        actual_inputs = type(self.inputs).current_identity

        def input_check(owner, *, environment):
            measured["full_input_checks"] += 1
            index = measured["full_input_checks"]
            emit("input-check-start", check=index)
            check_start = time.monotonic()
            passed = False
            try:
                result = actual_inputs(owner, environment=environment)
                passed = True
                return result
            finally:
                emit(
                    "input-check-complete",
                    check=index,
                    passed=passed,
                    check_seconds=time.monotonic() - check_start,
                )

        def native_process(argv, **kwargs):
            assert len(measured["commands"]) < 64
            if Path(argv[0]).name.startswith("excluded-"):
                label = "forbidden-excluded"
            elif Path(argv[0]).name.startswith("custom-"):
                label = "custom-harness"
            elif "--list" in argv:
                label = "libtest-list"
            elif "--format=pretty" in argv:
                label = "libtest-run"
            elif "metadata" in argv:
                label = "metadata"
            elif "check" in argv:
                label = "reviewed-gate"
            elif "--no-run" in argv:
                label = "test-compile"
            else:
                label = "compiler-runtime"
            index = len(measured["commands"]) + 1
            emit("process-start", command=index, label=label)
            result = actual_run(argv, **kwargs)
            measured["commands"].append(
                {
                    "label": label,
                    "exit_code": result.returncode,
                    "elapsed_seconds": result.elapsed_seconds,
                }
            )
            emit(
                "process-complete",
                command=index,
                label=label,
                exit_code=result.returncode,
                process_seconds=result.elapsed_seconds,
            )
            return result

        start = time.monotonic()
        try:
            with (
                patch.object(type(self.inputs), "current_identity", input_check),
                patch.object(executor, "run_bounded_process", native_process),
            ):
                receipt = admit_retained_cargo_consumer_v2(
                    self.materialized,
                    retirement_authority=authority,
                    acknowledge_source_retirement=True,
                    **self.execution(),
                )
        finally:
            measured["admission_wall_seconds"] = time.monotonic() - start
            print(
                "native receiving timing " + json.dumps(measured),
                file=sys.stderr,
                flush=True,
            )
        self.assertEqual(
            tuple(outcome.key for outcome in receipt.target_outcomes),
            tuple(target.key for target in expected_targets),
        )
        self.assertEqual(
            [outcome.kind for outcome in receipt.target_outcomes].count("libtest"),
            len(expected_libtests),
        )
        custom = [x for x in receipt.target_outcomes if x.kind == "custom-harness"]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0].passed_cases, ())
        excluded = [x for x in receipt.target_outcomes if x.kind == "excluded"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0].command_observation_identities, ())
        self.assertFalse(
            any(x["label"] == "forbidden-excluded" for x in measured["commands"])
        )
        self.assertEqual(
            sum(x["label"] == "custom-harness" for x in measured["commands"]),
            1,
        )
        self.assertEqual(
            sum(len(x.ignored_cases) for x in receipt.target_outcomes),
            sum(len(target.protocol.ignored) for target in expected_libtests),
        )
        receipt.require_declarations(
            self.reviewed.binding,
            self.reviewed.plan,
            self.reviewed.policy,
            self.reviewed.inventory,
        )
        self.assertFalse(self.original_path.exists())
        # Export only generic fixture contract identities and typed outcomes.
        # Parsing this observation does not reopen live admission authority.
        observation = {
            "receipt_identity": receipt.identity.uri,
            "inventory_identity": self.reviewed.inventory.identity.uri,
            "receipt": receipt.to_dict(),
        }
        encoded = json.dumps(observation, sort_keys=True)
        self.assertLessEqual(len(encoded.encode()), 65536)
        print("native receiving receipt " + encoded, file=sys.stderr, flush=True)

    def test_explicit_native_output_custody_keeps_complete_admission(self):
        native = RetainedCargoNativeCustodyPolicy(
            executables=RetainedCargoOutputBudget(512 * 1024**2, 1024**3),
            runtime=RetainedCargoOutputBudget(512 * 1024**2, 4 * 1024**3),
            build_outputs=RetainedCargoOutputBudget(256 * 1024**2, 1024**3),
        )
        self.open_case(native_custody=native)
        self.assertEqual(self.reviewed.policy.native_custody, native)
        receipt = admit_retained_cargo_consumer_v2(
            self.materialized,
            retirement_authority=self.retirement(),
            acknowledge_source_retirement=True,
            **self.execution(),
        )
        receipt.require_declarations(
            self.reviewed.binding,
            self.reviewed.plan,
            self.reviewed.policy,
            self.reviewed.inventory,
        )
        self.assertEqual(
            tuple(item.key for item in receipt.target_outcomes),
            tuple(item.key for item in self.reviewed.inventory.targets),
        )
        self.assertFalse(self.original_path.exists())

    def test_actual_original_source_is_preserved_and_refuses_retirement(self):
        self.open_case(retire=False)
        with self.assertRaisesRegex(ValueError, "expected-absence-changed"):
            self.retirement()
        self.assertEqual(self.original_path.read_bytes(), self.original_bytes)

    def test_cargo_home_addition_and_missing_authorization_refuse_before_execution(
        self,
    ):
        self.open_case()
        with self.assertRaisesRegex(ValueError, "authorization-required"):
            execute_retained_cargo_consumer_v2(
                self.materialized, **{**self.execution(), "allow_host_execution": False}
            )
        path = Path(self.fixture.environment["CARGO_HOME"]) / "foreign-input"
        path.write_bytes(b"uncaptured consumer input\n")
        try:
            with self.assertRaises(ValueError):
                execute_retained_cargo_consumer_v2(
                    self.materialized, **self.execution()
                )
        finally:
            path.unlink()

    def test_missing_retirement_acknowledgement_cannot_mint_receipt(self):
        self.open_case()
        with self.assertRaisesRegex(ValueError, "acknowledgement-required"):
            admit_retained_cargo_consumer_v2(
                self.materialized,
                retirement_authority=self.retirement(),
                acknowledge_source_retirement=False,
                **self.execution(),
            )

    def test_cargo_home_drift_after_native_process_refuses_next_gate(self):
        self.open_case()
        path = Path(self.fixture.environment["CARGO_HOME"]) / "late-input"
        actual = executor.run_bounded_process

        def changed(argv, **kwargs):
            result = actual(argv, **kwargs)
            path.write_bytes(b"changed after actual native metadata\n")
            return result

        try:
            with (
                patch.object(executor, "run_bounded_process", changed),
                self.assertRaises(RetainedCargoExecutionError) as failure,
            ):
                execute_retained_cargo_consumer_v2(
                    self.materialized, **self.execution()
                )
            self.assertEqual(len(failure.exception.observations), 1)
            self.assertEqual(failure.exception.observations[0].result.returncode, 0)
            self.assertEqual(
                failure.exception.observations[0].command.step_id,
                "retained-cargo-metadata",
            )
            self.assertEqual(
                path.read_bytes(), b"changed after actual native metadata\n"
            )
        finally:
            path.unlink(missing_ok=True)

    def test_permission_revoked_after_native_process_refuses_next_gate(self):
        self.open_case()
        actual = executor.run_bounded_process

        def revoked(argv, **kwargs):
            result = actual(argv, **kwargs)
            self.fixture.permission_path.write_bytes(b"review withdrawn\n")
            return result

        try:
            with (
                patch.object(executor, "run_bounded_process", revoked),
                self.assertRaises(RetainedCargoExecutionError) as failure,
            ):
                execute_retained_cargo_consumer_v2(
                    self.materialized, **self.execution()
                )
            self.assertEqual(len(failure.exception.observations), 1)
            self.assertEqual(failure.exception.observations[0].result.returncode, 0)
            self.assertIn(
                "exclusion-authority-changed", str(failure.exception.__cause__)
            )
        finally:
            self.fixture.permission_path.write_bytes(self.fixture.permission_bytes)

    def test_native_binary_change_after_listing_refuses_next_process(self):
        self.open_case()
        actual = executor.run_bounded_process
        changed = []
        unexpected_attempts = []
        marker = b"owned native test mutation"

        def changed_after_list(argv, **kwargs):
            if changed:
                unexpected_attempts.append(True)
                self.fail("no process may follow the mutated binary")
            result = actual(argv, **kwargs)
            if "--list" in argv:
                binary = Path(argv[0])
                output = (
                    self.materialized.scratch.context.workspace
                    / self.reviewed.plan.graph.common.output_directory
                )
                self.assertTrue(binary.resolve().is_relative_to(output.resolve()))
                descriptor = os.open(binary, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "ab") as stream:
                    stream.write(marker)
                    stream.flush()
                    os.fsync(stream.fileno())
                changed.append(binary)
            return result

        with (
            patch.object(executor, "run_bounded_process", changed_after_list),
            self.assertRaises(RetainedCargoExecutionError) as failure,
        ):
            execute_retained_cargo_consumer_v2(self.materialized, **self.execution())
        self.assertEqual(len(changed), 1)
        self.assertFalse(unexpected_attempts)
        self.assertTrue(changed[0].read_bytes().endswith(marker))
        self.assertTrue(
            failure.exception.observations[-1].command.step_id.endswith("-list")
        )
        self.assertEqual(failure.exception.observations[-1].result.returncode, 0)
        self.assertIn("executable changed", str(failure.exception.__cause__))
