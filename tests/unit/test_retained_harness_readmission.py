"""A reviewed readmission can replace an unqualified conversion harness."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import retained_harness_readmission as readmission
from literate_ai.adapters.harness_inventory import (
    HARNESS_BASELINE_SCHEMA,
    HARNESS_PARITY_SCHEMA,
)
from literate_ai.adapters.retained_harness_receipts import (
    retained_harness_receipt_policy,
)
from literate_ai.adapters.retained_harness_remote import RetainedHarnessRemoteResult
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.operator_adoption import ConversionAuthorityStage
from literate_ai.projects import load_project
from tests.unit.test_retained_harness_receipts import (
    _adapter,
    _invoke,
    _legacy_project,
    _selectors,
)
from tests.unit.test_retained_harness_remote import _worker


class RetainedHarnessReadmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        _legacy_project(self.root)
        _adapter().initialize(
            self.root,
            flavor_selectors=_selectors(self.root),
            source_intelligence_provider="none",
            convert=True,
            run_baseline=True,
        )
        self.source = self.root / "components/legacy-project-wrapper/implementation"

    def invoke(self, *arguments: str):
        return _invoke(
            "project",
            "retained-harness",
            "readmit",
            "--project",
            str(self.root),
            *arguments,
        )

    def make_unqualified_false_bazel_state(self) -> None:
        (self.source / "BUILD.bazel").write_text("# packaging stub\n", encoding="utf-8")
        inventory = {
            "schema": "urn:literate-ai:schema:v1:harness-inventory",
            "findings": [
                {
                    "detector_id": "build-system.bazel",
                    "path": "BUILD.bazel",
                    "detail": "Bazel workspace",
                }
            ],
            "commands": {
                "build": {
                    "id": "build",
                    "command": "bazel build //...",
                    "cwd": ".",
                    "evidence": "BUILD.bazel",
                    "cost": "local-cheap",
                },
                "test": {
                    "id": "test",
                    "command": "bazel test //...",
                    "cwd": ".",
                    "evidence": "BUILD.bazel",
                    "cost": "local-cheap",
                },
            },
            "stages": [
                {
                    "id": "build",
                    "command": "bazel build //...",
                    "cwd": ".",
                    "evidence": "BUILD.bazel",
                    "cost": "local-cheap",
                },
                {
                    "id": "test",
                    "command": "bazel test //...",
                    "cwd": ".",
                    "evidence": "BUILD.bazel",
                    "cost": "local-cheap",
                },
            ],
            "ci": {
                "configured": False,
                "class": "not-configured",
                "state": "not-configured",
            },
            "allow_unready": True,
        }
        (self.root / readmission.INVENTORY).write_text(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (self.root / readmission.BASELINE).write_text(
            json.dumps(
                {
                    "schema": HARNESS_BASELINE_SCHEMA,
                    "state": "skipped",
                    "phases": [],
                    "phase_count": 0,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / readmission.PARITY).write_text(
            json.dumps(
                {
                    "schema": HARNESS_PARITY_SCHEMA,
                    "state": "skipped",
                    "phases": [],
                    "phase_count": 0,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / "litai.harness.mk").write_text(
            "build:\n\tbazel build //...\ntest:\n\tbazel test //...\n",
            encoding="utf-8",
        )

    def admit_repo_man_test_stage(self) -> dict[str, object]:
        self.make_unqualified_false_bazel_state()
        driver = self.source / "repo.sh"
        driver.write_text("#!/bin/sh\nprintf 'Ran 1 test\\nOK\\n'\n", encoding="utf-8")
        driver.chmod(0o755)
        inventory_path = self.root / readmission.INVENTORY
        inventory = json.loads(inventory_path.read_bytes())
        retained = {
            "id": "test",
            "command": "./repo.sh test -c release",
            "cwd": ".",
            "evidence": "repo.sh",
            "cost": "host-heavy",
        }
        inventory["commands"]["test"] = retained
        inventory["stages"] = [
            retained if stage["id"] == "test" else stage
            for stage in inventory["stages"]
        ]
        inventory_path.write_text(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return retained

    def advance_authority(self, *stages: ConversionAuthorityStage) -> None:
        store = readmission.FilesystemConversionAuthorityStore(self.root)
        for stage in stages:
            store.advance(
                stage,
                evidence_identities=(
                    canonical_identity({"readmission-stage": stage.value}),
                ),
            )

    def test_readmission_replaces_false_bazel_and_requalifies_locally(self) -> None:
        self.make_unqualified_false_bazel_state()
        before_authority = (
            self.root / readmission.CONVERSION_AUTHORITY_FILE
        ).read_bytes()

        code, result = self.invoke()

        self.assertEqual(code, 0, result)
        plan = result["result"]
        self.assertTrue(plan["changes_required"])
        self.assertTrue(plan["commands_changed"])
        self.assertNotIn(
            "build-system.bazel",
            {item["detector_id"] for item in plan["new_inventory"]["findings"]},
        )
        code, result = self.invoke(
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
        )
        self.assertEqual(code, 0, result)
        self.assertTrue(result["result"]["applied"])
        prior = self.root / result["result"]["prior_evidence_history"]
        self.assertEqual(
            (prior / readmission.BASELINE).read_text(encoding="utf-8"),
            json.dumps(
                {
                    "schema": HARNESS_BASELINE_SCHEMA,
                    "state": "skipped",
                    "phases": [],
                    "phase_count": 0,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        inventory = json.loads((self.root / readmission.INVENTORY).read_bytes())
        self.assertEqual(
            json.loads((self.root / readmission.BASELINE).read_bytes())["state"],
            "passed",
        )
        self.assertEqual(
            json.loads((self.root / readmission.PARITY).read_bytes())["state"],
            "passed",
        )
        self.assertEqual(
            load_project(self.root).definition.test_receipt_policy,
            retained_harness_receipt_policy(inventory),
        )
        self.assertIn(
            "+flavor://legacy-adoption/build-legacy-shim",
            load_project(self.root).definition.default_flavor_selectors,
        )
        self.assertNotIn(
            "+flavor://literate-ai/build-bazel",
            load_project(self.root).definition.default_flavor_selectors,
        )
        lock = json.loads(
            (
                self.root / "components/legacy-project-wrapper/component.lock.json"
            ).read_bytes()
        )
        selected = lock["nodes"][0]["target_flavor_selection"]["slots"][0]["selected"]
        self.assertEqual(selected[0]["value"], "legacy-shim")
        self.assertEqual(
            (self.root / readmission.CONVERSION_AUTHORITY_FILE).read_bytes(),
            before_authority,
        )

    def test_retained_original_source_authority_can_readmit_commands(self) -> None:
        self.make_unqualified_false_bazel_state()
        self.advance_authority(ConversionAuthorityStage.RETAINED)
        before_authority = (
            self.root / readmission.CONVERSION_AUTHORITY_FILE
        ).read_bytes()

        code, planned = self.invoke()
        self.assertEqual(code, 0, planned)
        plan = planned["result"]
        self.assertEqual(plan["conversion_authority"]["stage"], "retained")
        self.assertEqual(
            plan["conversion_authority"]["release_authority"], "original-source"
        )
        code, applied = self.invoke(
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
        )

        self.assertEqual(code, 0, applied)
        self.assertTrue(applied["result"]["applied"])
        self.assertEqual(
            (self.root / readmission.CONVERSION_AUTHORITY_FILE).read_bytes(),
            before_authority,
        )

    def test_explicit_stage_retention_is_bound_into_reviewed_plan(self) -> None:
        retained = self.admit_repo_man_test_stage()

        code, without_retention = self.invoke()
        self.assertEqual(code, 0, without_retention)
        self.assertNotIn(
            "test",
            {
                stage["id"]
                for stage in without_retention["result"]["new_inventory"]["stages"]
            },
        )

        code, result = self.invoke("--retain-stage", "test")

        self.assertEqual(code, 0, result)
        plan = result["result"]
        self.assertEqual(plan["retained_stage_ids"], ["test"])
        self.assertIn(retained, plan["new_inventory"]["stages"])
        self.assertIn(
            {
                "detector_id": "operator.retained-stage",
                "path": "repo.sh",
                "detail": "previously admitted stage retained by explicit review: test",
            },
            plan["new_inventory"]["findings"],
        )

    def test_stage_retention_rejects_unknown_and_duplicate_selections(self) -> None:
        self.admit_repo_man_test_stage()

        code, unknown = self.invoke("--retain-stage", "missing")
        self.assertEqual(code, 2, unknown)
        self.assertEqual(
            unknown["error"]["code"], "retained_harness.stage_not_admitted"
        )

        code, duplicate = self.invoke(
            "--retain-stage", "test", "--retain-stage", "test"
        )
        self.assertEqual(code, 2, duplicate)
        self.assertEqual(
            duplicate["error"]["code"], "retained_harness.stage_selection_invalid"
        )

    def test_retaining_test_and_package_preserves_both_receipt_gates(self) -> None:
        test = self.admit_repo_man_test_stage()
        path = self.root / readmission.INVENTORY
        inventory = json.loads(path.read_bytes())
        package = {**test, "id": "package", "command": "./repo.sh package"}
        inventory["commands"]["package"] = package
        inventory["stages"].append(package)
        path.write_text(json.dumps(inventory), encoding="utf-8")

        code, result = self.invoke(
            "--retain-stage", "test", "--retain-stage", "package"
        )

        self.assertEqual(code, 0, result)
        plan = result["result"]
        self.assertEqual(plan["retained_stage_ids"], ["test", "package"])
        self.assertIn(test, plan["new_inventory"]["stages"])
        self.assertIn(package, plan["new_inventory"]["stages"])
        self.assertIn("package-result", plan["new_policy"]["required_evidence_kinds"])
        self.assertEqual(plan["new_policy"]["minimum_test_count"], 1)

    def test_apply_must_repeat_the_reviewed_stage_selection(self) -> None:
        self.admit_repo_man_test_stage()
        code, planned = self.invoke("--retain-stage", "test")
        self.assertEqual(code, 0, planned)

        code, result = self.invoke(
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            planned["result"]["plan_identity"],
        )

        self.assertEqual(code, 2, result)
        self.assertEqual(result["error"]["code"], "retained_harness.plan_stale")

    def test_specification_authority_refuses_readmission(self) -> None:
        self.advance_authority(
            ConversionAuthorityStage.RETAINED,
            ConversionAuthorityStage.DRAFTED,
            ConversionAuthorityStage.QUALIFIED,
        )
        before = (self.root / readmission.CONVERSION_AUTHORITY_FILE).read_bytes()

        code, result = self.invoke()

        self.assertEqual(code, 2, result)
        self.assertEqual(
            result["error"]["code"], "retained_harness.source_not_authoritative"
        )
        self.assertEqual(
            (self.root / readmission.CONVERSION_AUTHORITY_FILE).read_bytes(), before
        )

    def test_source_change_invalidates_reviewed_readmission_plan(self) -> None:
        self.make_unqualified_false_bazel_state()
        code, planned = self.invoke()
        self.assertEqual(code, 0, planned)
        plan = planned["result"]
        inventory_before = (self.root / readmission.INVENTORY).read_bytes()
        source = self.source / "app.py"
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        code, result = self.invoke(
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
        )

        self.assertEqual(code, 2, result)
        self.assertEqual(result["error"]["code"], "retained_harness.plan_stale")
        self.assertEqual(
            (self.root / readmission.INVENTORY).read_bytes(), inventory_before
        )
        self.assertFalse((self.root / readmission.HISTORY).exists())

    def test_source_change_during_qualification_rolls_back_publication(self) -> None:
        self.make_unqualified_false_bazel_state()
        code, planned = self.invoke()
        self.assertEqual(code, 0, planned)
        plan = planned["result"]
        inventory_before = (self.root / readmission.INVENTORY).read_bytes()
        original = readmission._parity_report

        def mutate_after_parity(*args, **kwargs):
            result = original(*args, **kwargs)
            source = self.source / "app.py"
            source.write_text(
                source.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            return result

        with mock.patch.object(
            readmission, "_parity_report", side_effect=mutate_after_parity
        ):
            code, result = self.invoke(
                "--apply",
                "--acknowledge",
                "--expected-plan-identity",
                plan["plan_identity"],
            )

        self.assertEqual(code, 2, result)
        self.assertEqual(result["error"]["code"], "retained_harness.source_changed")
        self.assertEqual(
            (self.root / readmission.INVENTORY).read_bytes(), inventory_before
        )
        history = self.root / readmission.HISTORY
        self.assertFalse(history.exists() and any(history.rglob("*")))

    def test_readmission_requires_review_and_rolls_back_publication(self) -> None:
        self.make_unqualified_false_bazel_state()
        code, result = self.invoke()
        self.assertEqual(code, 0, result)
        plan = result["result"]
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
            and ".litai-locks" not in path.parts
            and "perf" not in path.parts
        }
        code, result = self.invoke(
            "--apply",
            "--expected-plan-identity",
            plan["plan_identity"],
        )
        self.assertEqual(code, 2, result)
        self.assertEqual(
            result["error"]["code"], "retained_harness.acknowledgement_required"
        )
        with mock.patch.object(
            readmission.ProjectConfigurationStore,
            "update",
            side_effect=OSError("fault"),
        ):
            code, result = self.invoke(
                "--apply",
                "--acknowledge",
                "--expected-plan-identity",
                plan["plan_identity"],
            )
        self.assertEqual(code, 2, result)
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
            and ".litai-locks" not in path.parts
            and "perf" not in path.parts
        }
        self.assertEqual(after, before)

    def test_allow_unready_policy_is_derived_from_final_inventory(self) -> None:
        root = self.base / "allow-unready"
        _legacy_project(root)

        _adapter().initialize(
            root,
            flavor_selectors=_selectors(root),
            source_intelligence_provider="none",
            convert=True,
            allow_unready=True,
        )

        inventory = json.loads((root / readmission.INVENTORY).read_bytes())
        self.assertTrue(inventory["allow_unready"])
        self.assertEqual(
            load_project(root).definition.test_receipt_policy,
            retained_harness_receipt_policy(inventory),
        )

    def test_remote_readmission_executes_direct_and_generated_wrapper(self) -> None:
        retained = self.admit_repo_man_test_stage()
        worker = _worker()
        catalog_identity = canonical_identity({"catalog": "fixture"})

        class Executor:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []

            def execute(self, selected_worker, **kwargs):
                inventory = kwargs["inventory"]
                self.commands.append(
                    [stage["command"] for stage in inventory["stages"]]
                )
                phases = []
                for stage in inventory["stages"]:
                    collection = (
                        {
                            "state": "nonempty",
                            "total": 1,
                            "passed": 1,
                            "failed": 0,
                            "skipped": 0,
                            "known_failed": 0,
                        }
                        if stage["id"].split(".", 1)[0] == "test"
                        else {
                            "state": "not-applicable",
                            "total": None,
                            "passed": None,
                            "failed": None,
                            "skipped": None,
                            "known_failed": None,
                        }
                    )
                    phases.append(
                        {
                            "phase": stage["id"],
                            "command": stage["command"],
                            "exit_code": 0,
                            "timed_out": False,
                            "tree": {"identity": "sha256:" + "1" * 64},
                            "source_tree": {"identity": "sha256:" + "2" * 64},
                            "test_collection": collection,
                        }
                    )
                return RetainedHarnessRemoteResult(
                    canonical_identity({"request": len(self.commands)}),
                    kwargs["lifecycle_request_identity"],
                    selected_worker.identity,
                    canonical_identity(inventory),
                    kwargs["source_identity"],
                    {
                        "operating_system": "linux",
                        "operating_system_release": "fixture",
                        "machine": "x86_64",
                        "python_implementation": "cpython",
                        "python_version": "3.12",
                    },
                    tuple(phases),
                )

        executor = Executor()
        plan = readmission.plan_retained_harness_readmission(
            self.root,
            execution_worker=worker,
            worker_catalog_identity=catalog_identity,
            retained_stage_ids=("test",),
        )

        self.assertIn(retained, plan["new_inventory"]["stages"])

        result = readmission.apply_retained_harness_readmission(
            self.root,
            expected_plan_identity=plan["plan_identity"],
            acknowledge=True,
            timeout_seconds=1800,
            execution_worker=worker,
            worker_catalog_identity=catalog_identity,
            retained_stage_ids=("test",),
            remote_executor=executor,
        )

        self.assertTrue(result["applied"])
        self.assertEqual(len(executor.commands), 2)
        self.assertFalse(
            any("litai.harness.mk" in item for item in executor.commands[0])
        )
        self.assertTrue(
            all("litai.harness.mk" in item for item in executor.commands[1])
        )
