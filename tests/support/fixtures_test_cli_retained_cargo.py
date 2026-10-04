"""Shared test fixtures extracted from test_cli_retained_cargo."""

import io
import json
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.cli import main
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.retained_cargo_admission import (
    RetainedCargoAdmissionReceipt,
    RetainedCargoSourceRetirement,
)
from literate_ai.contracts.retained_cargo_tests import (
    RetainedCargoTestInventory,
    RetainedCargoTestTarget,
    retained_cargo_test_targets,
)
from tests.support import fixtures_test_retained_cargo_current as current_fixtures
from tests.support import (
    fixtures_test_retained_cargo_materialization as materialization_fixtures,
)
from tests.support.fixtures_test_retained_cargo_files import blob


class RetainedCargoCliTests(unittest.TestCase):
    def setUp(self):
        fixture = materialization_fixtures.RetainedCargoMaterializationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.self_update = self.enterContext(
            patch("literate_ai.cli.dispatch.maybe_host_self_update")
        )
        self.root = fixture.root
        self.inputs = fixture.fixture.inputs
        gates = current_fixtures.RetainedCargoCurrentTests()
        gates.setUp()
        self.addCleanup(gates.doCleanups)
        plan = replace(
            gates.plan,
            importer_project_id=self.inputs.importer_project_id,
            commands=self.inputs.current_gates,
            toolchains=tuple(
                sorted(
                    (
                        self.inputs.current_cargo_identity,
                        self.inputs.current_rustc_identity,
                    ),
                    key=lambda item: item.uri,
                )
            ),
        )
        content = canonical_json_bytes(plan.to_dict())
        (self.root / "gates.json").write_bytes(content)
        self.gate_ref = blob(content)
        self.archive = self.root / "qualified.zip"
        self.archive.write_bytes(fixture.fixture.archive)
        self.arguments = [
            "--project",
            str(self.root),
            "--binding",
            "binding.json",
            "--plan",
            "plan.json",
            "--gates",
            "gates.json",
            "--reviewed-binding",
            blob(fixture.binding_bytes).identity,
            "--binding-size",
            str(len(fixture.binding_bytes)),
            "--reviewed-gates",
            self.gate_ref.identity,
            "--gate-size",
            str(self.gate_ref.size),
            "--provider-project",
            str(self.root),
            "--provider-component",
            "components/provider",
            "--provider-profile",
            "profile.json",
            "--provider-id",
            "fixture",
            "--archive",
            str(self.archive),
            "--store-id",
            "reviewed-store",
        ]
        self.provider_reader = self.enterContext(
            patch(
                "literate_ai.cli.retained_cargo.read_retained_provider_authority",
                return_value=SimpleNamespace(),
            )
        )
        native = SimpleNamespace(require_unchanged=lambda: None)
        self.enterContext(
            patch(
                "literate_ai.cli.retained_cargo.read_retained_provider_native",
                return_value=native,
            )
        )

        def compose(importer, native):
            self.assertEqual(
                importer.reviewed_binding_identity,
                self.inputs.reviewed_binding_identity,
            )
            self.assertEqual(importer.gates.commands, self.inputs.current_gates)
            importer.require_unchanged()
            native.require_unchanged()
            return replace(self.inputs, require_unchanged=importer.require_unchanged)

        self.enterContext(
            patch(
                "literate_ai.cli.retained_cargo.compose_retained_cargo_current_inputs",
                side_effect=compose,
            )
        )
        self.enterContext(
            patch.dict("os.environ", {"OBJ_DIR": "pkgs", "BUILD_DIR": "build"})
        )

    def admission_arguments(self):
        plan = self.fixture.fixture.plan
        gates = current_fixtures.RetainedCargoCurrentTests()
        gates.setUp()
        self.addCleanup(gates.doCleanups)
        policy = replace(
            gates.plan,
            importer_project_id=self.inputs.importer_project_id,
            commands=self.inputs.current_gates,
            toolchains=tuple(
                sorted(
                    (
                        self.inputs.current_cargo_identity,
                        self.inputs.current_rustc_identity,
                    ),
                    key=lambda item: item.uri,
                )
            ),
        )
        targets = tuple(
            sorted(
                (
                    RetainedCargoTestTarget(
                        package.root,
                        target.name,
                        tuple(sorted(target.kinds)),
                        ("reviewed_case",),
                    )
                    for package, target in retained_cargo_test_targets(plan.graph)
                ),
                key=lambda item: item.key,
            )
        )
        inventory = RetainedCargoTestInventory(
            self.inputs.importer_project_id, plan.identity, policy.identity, targets
        )
        retirement = RetainedCargoSourceRetirement(
            self.inputs.importer_project_id,
            self.fixture.fixture.binding.identity,
            ("retired/provider",),
        )
        contents = {}
        for name, value in (("tests.json", inventory), ("retirement.json", retirement)):
            content = canonical_json_bytes(value.to_dict())
            (self.root / name).write_bytes(content)
            contents[name] = content
        return [
            *self.arguments,
            "--tests",
            "tests.json",
            "--reviewed-tests",
            blob(contents["tests.json"]).identity,
            "--tests-size",
            str(len(contents["tests.json"])),
            "--retirement",
            "retirement.json",
            "--reviewed-retirement",
            blob(contents["retirement.json"]).identity,
            "--retirement-size",
            str(len(contents["retirement.json"])),
            "--allow-host-execution",
            "--acknowledge-source-retirement",
            "--offline",
        ]

    def invoke(self, operation, arguments=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        status = main(
            [
                "project",
                "retained-cargo",
                operation,
                *(self.arguments if arguments is None else arguments),
            ],
            stdout=stdout,
            stderr=stderr,
        )
        self.self_update.assert_not_called()
        return status, stdout.getvalue(), stderr.getvalue()

    def snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_check_absent_packages_is_read_only_and_does_not_admit(self):
        before = self.snapshot()
        status, output, error = self.invoke("check")
        self.assertEqual((status, error), (0, ""), error)
        result = json.loads(output)["result"]
        self.assertEqual(result["missing_packages"], len(self.fixture.destinations))
        self.assertEqual(result["verified_present_packages"], 0)
        self.assertFalse(result["consumer_gates_executed"])
        self.assertFalse(result["importer_admission"])
        self.assertEqual(before, self.snapshot())

    def test_materialize_then_check_complete_packages_and_reuse_without_rewrites(self):
        for operation in ("materialize", "check", "materialize"):
            status, output, error = self.invoke(operation)
            self.assertEqual((status, error), (0, ""), error)
            result = json.loads(output)["result"]
            self.assertEqual(result["missing_packages"], 0)
            self.assertEqual(
                result["verified_present_packages"], len(self.fixture.destinations)
            )
            self.assertFalse(result["source_retirement"])
            current = {
                p: (p.stat().st_ino, p.read_bytes())
                for d in self.fixture.destinations
                for p in d.rglob("*")
                if p.is_file()
            }
            if operation == "materialize" and not hasattr(self, "published"):
                self.published = current
            self.assertEqual(current, self.published)

    def test_wrong_binding_review_refuses_before_provider_discovery(self):
        arguments = list(self.arguments)
        arguments[arguments.index("--reviewed-binding") + 1] = "sha256:" + "0" * 64
        before = self.snapshot()
        status, _, error = self.invoke("check", arguments)
        self.assertEqual(status, 2, error)
        self.provider_reader.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_gate_drift_refuses_before_provider_discovery(self):
        path = self.root / "gates.json"
        path.write_bytes(path.read_bytes() + b" ")
        status, _, error = self.invoke("check")
        self.assertEqual(status, 2, error)
        self.provider_reader.assert_not_called()

    def test_wrong_archive_is_not_materialized_and_diagnostics_are_sanitized(self):
        self.archive.write_bytes(b"secret-shaped candidate payload")
        status, output, error = self.invoke("materialize")
        self.assertEqual(status, 2, error)
        self.assertNotIn("secret-shaped", error + output)
        self.assertNotIn(str(self.root), error + output)
        self.assertTrue(all(not p.exists() for p in self.fixture.destinations))

    def test_check_refuses_foreign_package_files_without_removing_them(self):
        for destination in self.fixture.destinations:
            self.fixture.populate(destination)
        foreign = self.fixture.destinations[0] / "foreign"
        foreign.write_bytes(b"preserve")
        before = self.snapshot()
        status, _, error = self.invoke("check")
        self.assertEqual(status, 2, error)
        self.assertEqual(before, self.snapshot())

    def test_missing_review_arguments_are_usage_error(self):
        status, _, error = self.invoke("materialize", [])
        self.assertEqual(status, 2, error)
        self.provider_reader.assert_not_called()

    def test_admit_returns_canonical_receipt_after_explicit_acknowledgements(self):
        arguments = self.admission_arguments()
        observed = SimpleNamespace(
            command=(sys.executable,),
            identity=self.inputs.current_cargo_identity.uri,
            require_unchanged=lambda: None,
        )
        receipt = RetainedCargoAdmissionReceipt(
            self.fixture.fixture.binding.identity,
            self.fixture.fixture.plan.identity,
            canonical_identity("gate policy"),
            canonical_identity("test inventory"),
            canonical_identity("source retirement"),
            self.fixture.fixture.plan.identity,
            self.fixture.fixture.binding.qualification_identity,
            (self.fixture.fixture.plan.identity,),
        )
        with (
            patch(
                "literate_ai.cli.retained_cargo.discover_cargo_toolchain",
                return_value=observed,
            ),
            patch(
                "literate_ai.cli.retained_cargo.discover_rust_toolchain",
                return_value=SimpleNamespace(
                    command=(sys.executable,),
                    identity=self.inputs.current_rustc_identity.uri,
                    require_unchanged=lambda: None,
                ),
            ),
            patch(
                "literate_ai.cli.retained_cargo.discover_make_toolchain",
                return_value=observed,
            ),
            patch(
                "literate_ai.cli.retained_cargo.read_retained_cargo_execution_inputs",
                return_value=SimpleNamespace(),
            ),
            patch(
                "literate_ai.cli.retained_cargo.admit_retained_cargo_consumer",
                return_value=receipt,
            ) as admit,
        ):
            status, output, error = self.invoke("admit", arguments)
        self.assertEqual((status, error), (0, ""), error)
        result = json.loads(output)["result"]
        self.assertTrue(result["consumer_gates_executed"])
        self.assertTrue(result["importer_admission"])
        self.assertTrue(result["source_retirement"])
        self.assertEqual(result["admission_identity"], receipt.identity.uri)
        admit.assert_called_once()

    def test_admit_requires_both_acknowledgements_before_materialization(self):
        arguments = self.admission_arguments()
        for option in ("--allow-host-execution", "--acknowledge-source-retirement"):
            selected = [item for item in arguments if item != option]
            with self.subTest(option=option):
                status, _, error = self.invoke("admit", selected)
                self.assertEqual(status, 2, error)
                self.assertTrue(
                    all(not path.exists() for path in self.fixture.destinations)
                )
        self.provider_reader.assert_not_called()
