"""End-to-end retained-harness receipt and promotion regressions."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.harness_inventory import (
    HARNESS_PARITY_SCHEMA,
    execute_retained_harness,
)
from literate_ai.adapters.project_initialization import (
    detect_repo_flavors,
    host_platform_selector,
    record_project_authority_review,
)
from literate_ai.adapters.retained_evidence_migration import (
    LEGACY_WRAPPER_PARITY_SCHEMA_V1,
    migrate_retained_evidence,
)
from literate_ai.adapters.retained_harness_receipts import (
    retained_harness_receipt_policy,
    retained_harness_worker_identity,
)
from literate_ai.adapters.retained_harness_remote import (
    RetainedHarnessRemoteError,
    RetainedHarnessRemoteResult,
)
from literate_ai.cli import main
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    ProjectInitializationOrigin,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.projects import PROJECT_FILENAME
from tests.support.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)


def _origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        repository_url="ssh://git.example.test/operator/literate-ai.git",
        git_revision="c" * 40,
        distribution_name="literate-ai",
        distribution_version="0.9.0",
    )


def _adapter() -> FilesystemProjectInitializationAdapter:
    return FilesystemProjectInitializationAdapter(
        initialization_origin_provider=_origin,
        standard_binding_provider=lambda: None,
    )


def _selectors(target: Path) -> tuple[str, ...]:
    return (
        *(f"+{selector}" for selector in detect_repo_flavors(target)),
        host_platform_selector(),
    )


def _invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    status = main(arguments, stdout=stdout, stderr=stderr)
    content = stdout.getvalue() if status == 0 else stderr.getvalue()
    return status, json.loads(content)


def _legacy_project(target: Path, test_summary: str = "Ran 2 tests") -> None:
    target.mkdir()
    (target / "app.py").write_text("print('retained')\n", encoding="utf-8")
    (target / "Makefile").write_text(
        "build:\n"
        "\tmkdir -p build\n"
        "\tprintf 'artifact\\n' > build/app\n"
        "test:\n"
        f"\t@printf '{test_summary}\\n'\n"
        "\t@printf 'OK\\n'\n"
        "package:\n"
        "\tmkdir -p dist\n"
        "\tprintf 'package\\n' > dist/app.txt\n",
        encoding="utf-8",
    )


def _write_workers(path: Path, *workers: ExecutionWorker) -> None:
    path.write_bytes(
        canonical_json_bytes(ExecutionWorkerCatalog(tuple(workers)).to_dict())
    )


class RetainedHarnessReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Initialize the default converted project once; tests copy it.
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.template = Path(temporary.name).resolve() / "converted"
        _legacy_project(cls.template)
        cls.initialized = _adapter().initialize(
            cls.template,
            flavor_selectors=_selectors(cls.template),
            source_intelligence_provider="none",
            convert=True,
        )

    def test_passing_retained_harness_emits_and_promotes_finalized_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "converted"
            shutil.copytree(self.template, project, symlinks=True)
            candidate = root / "candidate.json"

            status, run = _invoke(
                "project",
                "test-receipt",
                "run-retained",
                str(candidate),
                "--project",
                str(project),
                "--worker-id",
                "local",
            )

            self.assertEqual(status, 0, run)
            self.assertEqual(self.initialized["standard_binding"], "retained-harness")
            result = run["result"]
            self.assertEqual(result["classification"], "retained-legacy-parity")
            self.assertEqual(result["tests"], 2)
            self.assertFalse(result["native_component_generation"])
            self.assertFalse(result["native_component_acceptance"])
            finalized = json.loads(candidate.read_text(encoding="utf-8"))
            receipt = finalized["receipt"]
            self.assertEqual(receipt["suite"]["id"], "retained-legacy-parity")
            self.assertNotIn("acceptance-result", receipt["evidence"])
            self.assertNotIn("generation-provenance", receipt["evidence"])
            self.assertIn("build-result", receipt["evidence"])
            self.assertIn("package-result", receipt["evidence"])
            evidence = json.loads(Path(result["evidence"]).read_text(encoding="utf-8"))
            self.assertEqual(evidence["tests"], 2)
            self.assertTrue(
                all("stdout_excerpt" not in item for item in evidence["phases"])
            )
            self.assertEqual(
                [item["command"] for item in evidence["phases"]],
                [
                    "make -f Makefile",
                    "make -f Makefile test",
                    "make -f Makefile package",
                ],
            )

            status, updated = _invoke(
                "project",
                "test-receipt",
                "update",
                str(candidate),
                "--project",
                str(project),
            )
            self.assertEqual(status, 0, updated)
            status, current = _invoke(
                "project",
                "test-receipt",
                "require-current",
                "--project",
                str(project),
            )
            self.assertEqual(status, 0, current)
            self.assertEqual(current["result"]["state"], "current")

            implementation = (
                project / "components/legacy-project-wrapper/implementation/app.py"
            )
            implementation.write_text("print('changed')\n", encoding="utf-8")
            status, stale = _invoke(
                "project",
                "test-receipt",
                "require-current",
                "--project",
                str(project),
            )
            self.assertEqual(status, 2, stale)
            self.assertEqual(stale["error"]["code"], "project.test_receipt_stale")

    def test_remote_worker_is_selected_exactly_and_finalized_by_coordinator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "converted"
            shutil.copytree(self.template, project, symlinks=True)
            worker = ExecutionWorker(
                "ubuntu-24.04-nvidia",
                ExecutionWorkerKind.SSH,
                endpoint="runner@worker.example",
                workspace="~/literate-ai",
                requirements=ExecutionRequirements(os_family="linux"),
            )
            catalog_path = root / "workers.json"
            _write_workers(catalog_path, worker)
            candidate = root / "candidate.json"

            def execute(_self, selected, **values):
                self.assertEqual(selected, worker)
                self.assertEqual(
                    values["worker_catalog_identity"],
                    ExecutionWorkerCatalog((worker,)).identity,
                )
                report = execute_retained_harness(
                    values["inventory"],
                    legacy_root=values["source_root"],
                    timeout_seconds=values["timeout_seconds"],
                )
                phases = tuple(
                    {
                        key: value
                        for key, value in phase.items()
                        if key not in {"stdout_excerpt", "stderr_excerpt"}
                    }
                    for phase in report["phases"]
                )
                return RetainedHarnessRemoteResult(
                    canonical_identity({"mock": "remote-request"}),
                    values["lifecycle_request_identity"],
                    selected.identity,
                    canonical_identity(values["inventory"]),
                    values["source_identity"],
                    {
                        "operating_system": "linux",
                        "operating_system_release": "6.8.0",
                        "machine": "x86_64",
                        "python_implementation": "cpython",
                        "python_version": "3.13.7",
                    },
                    phases,
                )

            with mock.patch(
                "literate_ai.adapters.retained_harness_receipts."
                "RetainedHarnessSshExecutor.execute",
                autospec=True,
                side_effect=execute,
            ) as dispatched:
                status, result = _invoke(
                    "project",
                    "test-receipt",
                    "run-retained",
                    str(candidate),
                    "--project",
                    str(project),
                    "--worker-id",
                    worker.worker_id,
                    "--worker-config",
                    str(catalog_path),
                )

            self.assertEqual(status, 0, result)
            dispatched.assert_called_once()
            evidence = json.loads(
                Path(result["result"]["evidence"]).read_text(encoding="utf-8")
            )
            self.assertEqual(evidence["worker_id"], worker.worker_id)
            self.assertEqual(evidence["platform"]["operating_system"], "linux")
            self.assertEqual(evidence["platform"]["machine"], "x86_64")
            self.assertEqual(
                evidence["worker_identity"],
                retained_harness_worker_identity(worker.worker_id, worker).uri,
            )

    def test_remote_receiver_failure_exposes_no_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "converted"
            shutil.copytree(self.template, project, symlinks=True)
            worker = ExecutionWorker(
                "linux",
                ExecutionWorkerKind.SSH,
                endpoint="runner@linux.example",
                workspace="~/literate-ai",
                requirements=ExecutionRequirements(os_family="linux"),
            )
            catalog_path = root / "workers.json"
            _write_workers(catalog_path, worker)
            candidate = root / "candidate.json"
            with mock.patch(
                "literate_ai.adapters.retained_harness_receipts."
                "RetainedHarnessSshExecutor.execute",
                side_effect=RetainedHarnessRemoteError(
                    "retained_receipt.remote_execution_failed",
                    "receiver reported no passing result: "
                    + ("bounded diagnostic " * 40)
                    + "retained-tail",
                ),
            ):
                status, result = _invoke(
                    "project",
                    "test-receipt",
                    "run-retained",
                    str(candidate),
                    "--project",
                    str(project),
                    "--worker-id",
                    worker.worker_id,
                    "--worker-config",
                    str(catalog_path),
                )

            self.assertEqual(status, 2, result)
            self.assertEqual(
                result["error"]["code"],
                "retained_receipt.remote_execution_failed",
            )
            self.assertIn("retained-tail", result["error"]["message"])
            self.assertFalse(candidate.exists())
            self.assertFalse((root / "candidate.evidence.json").exists())

    def test_harness_cannot_mutate_authored_source_while_earning_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "converted"
            shutil.copytree(self.template, project, symlinks=True)
            implementation = (
                project / "components/legacy-project-wrapper/implementation"
            )
            (implementation / "Makefile").write_text(
                "build:\n"
                "\tprintf '# generated\\n' >> app.py\n"
                "test:\n"
                "\t@printf 'Ran 2 tests\\n'\n"
                "\t@printf 'OK\\n'\n"
                "package:\n"
                "\t@true\n",
                encoding="utf-8",
            )
            candidate = root / "candidate.json"

            status, rejected = _invoke(
                "project",
                "test-receipt",
                "run-retained",
                str(candidate),
                "--project",
                str(project),
            )

            self.assertEqual(status, 2, rejected)
            self.assertEqual(
                rejected["error"]["code"], "retained_receipt.source_mutated"
            )
            self.assertFalse(candidate.exists())
            self.assertFalse((root / "candidate.evidence.json").exists())

    def test_nonpassing_or_collection_only_count_cannot_become_receipt(self):
        for summary in ("1 passed, 1 xfailed", "collected 8 items"):
            with self.subTest(summary=summary):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    project = root / "converted"
                    _legacy_project(project, summary)
                    _adapter().initialize(
                        project,
                        flavor_selectors=_selectors(project),
                        source_intelligence_provider="none",
                        convert=True,
                    )
                    candidate = root / "candidate.json"

                    status, rejected = _invoke(
                        "project",
                        "test-receipt",
                        "run-retained",
                        str(candidate),
                        "--project",
                        str(project),
                    )

                    self.assertEqual(status, 2, rejected)
                    self.assertEqual(
                        rejected["error"]["code"],
                        "retained_receipt.tests_not_all_passing",
                    )
                    self.assertFalse(candidate.exists())
                    self.assertFalse((root / "candidate.evidence.json").exists())

    def test_update_migrates_0_9_0_retained_evidence_before_run_retained(self):
        # Reproduces issue #342: a project converted and Phase 1-qualified
        # under 0.9.0 recorded qualified parity at schema @1 and a
        # harness-inventory.json with no source_scope. 0.11.0's run-retained
        # front door refuses both until they are migrated.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "converted"
            shutil.copytree(self.template, project, symlinks=True)

            parity_path = project / ".literate" / "legacy-wrapper-parity.json"
            parity_document = json.loads(parity_path.read_text(encoding="utf-8"))
            self.assertEqual(parity_document["schema"], HARNESS_PARITY_SCHEMA)
            self.assertEqual(parity_document["state"], "passed")
            parity_document["schema"] = LEGACY_WRAPPER_PARITY_SCHEMA_V1
            parity_path.write_bytes(canonical_json_bytes(parity_document) + b"\n")

            inventory_path = project / ".literate" / "harness-inventory.json"
            inventory_document = json.loads(inventory_path.read_text(encoding="utf-8"))
            original_source_scope = inventory_document["source_scope"]
            self.assertIsInstance(original_source_scope, dict)
            del inventory_document["source_scope"]
            inventory_path.write_bytes(canonical_json_bytes(inventory_document) + b"\n")

            # Mirror issue #342's reproduction step 3: rebind the test receipt
            # policy emitted by retained_harness_receipt_policy(inventory) for
            # the now-0.9.0-shaped inventory, so the only gaps under test are
            # the parity schema and the missing source scope.
            project_path = project / PROJECT_FILENAME
            project_document = json.loads(project_path.read_text(encoding="utf-8"))
            project_document["test_receipt_policy"] = retained_harness_receipt_policy(
                inventory_document
            ).to_dict()
            project_path.write_bytes(canonical_json_bytes(project_document) + b"\n")
            record_project_authority_review(project)

            candidate = root / "candidate.json"
            status, blocked = _invoke(
                "project",
                "test-receipt",
                "run-retained",
                str(candidate),
                "--project",
                str(project),
                "--worker-id",
                "local",
            )
            self.assertEqual(status, 2, blocked)
            self.assertEqual(
                blocked["error"]["code"], "retained_receipt.parity_unqualified"
            )

            migration = migrate_retained_evidence(project)

            self.assertIn(".literate/legacy-wrapper-parity.json", migration.migrated)
            self.assertIn(".literate/harness-inventory.json", migration.migrated)

            migrated_parity = json.loads(parity_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated_parity["schema"], HARNESS_PARITY_SCHEMA)
            self.assertEqual(migrated_parity["state"], "passed")

            migrated_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            self.assertEqual(
                migrated_inventory["source_scope"]["paths"],
                original_source_scope["paths"],
            )
            self.assertEqual(
                migrated_inventory["source_scope"]["schema"],
                "literate-ai/retained-source-scope@1",
            )

            status, run = _invoke(
                "project",
                "test-receipt",
                "run-retained",
                str(candidate),
                "--project",
                str(project),
                "--worker-id",
                "local",
            )
            self.assertEqual(status, 0, run)
            self.assertEqual(run["result"]["classification"], "retained-legacy-parity")

            # Migrating an already-current project is a no-op.
            self.assertEqual(migrate_retained_evidence(project).migrated, ())


if __name__ == "__main__":
    unittest.main()
