"""Real completed runs become local retained admission and exact payload custody."""

import os
import shutil
import subprocess
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.adapters.retained_project_inputs import (
    _remove_tree,
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_receipt import (
    AdmittedRetainedProject,
    admit_retained_project,
)
from literate_ai.contracts import BlobRef, ProjectTestReceipt
from literate_ai.contracts.retained_project_receipt import RetainedProjectReceipt
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.storage.cas import BlobIntegrityError, FileSystemCAS
from tests.critical import test_retained_project_completion as completed
from tests.critical import test_retained_project_execution as commands
from tests.critical import test_retained_project_sdks as sdk_fixtures


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
)
class RetainedProjectReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        commands.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(commands.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.fixture = completed.RetainedProjectCompletionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = FileSystemCAS(self.fixture.fixture.base / "retained-cas")

    def finish(self):
        workspace, _ = self.fixture.finish()
        return workspace, admit_retained_project(workspace, self.store)

    def refresh_inputs(self, roles):
        f = self.fixture.fixture
        f.manifest = discover_retained_project_inputs(
            f.source, roles, f.manifest.limits, allow_internal_symlinks=True
        )
        f.plan = replace(f.plan, manifest_identity=f.manifest.identity)
        f.current = f.plan
        self.fixture.graph = project_retained_project_managed_graph(
            f.plan, self.fixture.locks, version="1.0.0"
        )

    def test_native_exports_survive_scratch_retirement_with_original_receipt_authority(
        self,
    ):
        workspace, admission = self.finish()
        receipt = admission.receipt
        self.assertEqual(RetainedProjectReceipt.from_dict(receipt.to_dict()), receipt)
        self.assertEqual(receipt.passed, 1)
        self.assertEqual(receipt.skipped, 0)
        self.assertEqual(receipt.root("plan").identity, workspace.plan.identity.uri)
        self.assertEqual(receipt.to_dict()["source_authority"], "original")
        self.assertFalse(receipt.to_dict()["conversion_advanced"])
        self.assertFalse(receipt.to_dict()["release_authorized"])
        with self.assertRaises(ValueError):
            ProjectTestReceipt.from_dict(receipt.to_dict())
        with self.assertRaisesRegex(ValueError, "live-admission-required"):
            AdmittedRetainedProject(
                receipt,
                admission.run,
                self.store,
                workspace.source,
                workspace.current_plan,
            )
        _remove_tree(workspace.root)
        destination = self.fixture.fixture.base / "package-inputs"
        manifest = admission.materialize_payload(destination)
        self.assertEqual(
            tuple(m.path for m in manifest.members), ("app.bin", "docs.txt")
        )
        self.assertFalse((destination / "test-result.json").exists())
        result = subprocess.run(
            (str(destination / "app.bin"), "echo"),
            input=b"retained\x00payload",
            capture_output=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(result.stdout, b"retained\x00payload")
        self.assertEqual((destination / "app.bin").stat().st_mode & 0o777, 0o755)

    def test_incomplete_run_cannot_publish_a_receipt(self):
        workspace = self.fixture.workspace()
        with self.assertRaisesRegex(ValueError, "run-incomplete"):
            admit_retained_project(workspace, self.store)
        self.assertEqual(list(self.store.iter_refs()), [])

    def test_original_source_and_changed_plan_invalidate_package_consumption(self):
        _, admission = self.finish()
        f = self.fixture.fixture
        original = f.source / "src/main.txt"
        content = original.read_bytes()
        original.write_bytes(b"changed source")
        with self.assertRaises(ValueError):
            admission.require_current()
        original.write_bytes(content)
        verifier = f.source / ACCEPTANCE_POLICY_PATH
        policy_bytes = verifier.read_bytes()
        verifier.write_bytes(policy_bytes + b"\n")
        with self.assertRaises(ValueError):
            admission.require_current()
        verifier.write_bytes(policy_bytes)
        f.current = replace(f.plan, project_id="other project")
        with self.assertRaisesRegex(ValueError, "plan-changed"):
            admission.require_current()

    def test_corrupt_retained_payload_or_metadata_blocks_materialization(self):
        _, admission = self.finish()
        member = next(
            m for m in admission.payload_manifest.members if m.path == "app.bin"
        )
        blob = self.store.path_for(BlobRef(member.sha256, member.size))
        blob.write_bytes(b"corrupt retained executable")
        destination = self.fixture.fixture.base / "package-inputs"
        with self.assertRaises(BlobIntegrityError):
            admission.materialize_payload(destination)
        self.assertFalse(destination.exists())

    def test_concurrent_existing_destination_is_preserved(self):
        _, admission = self.finish()
        destination = self.fixture.fixture.base / "existing"
        destination.mkdir()
        (destination / "keep.txt").write_bytes(b"peer-owned")
        with self.assertRaises(FileExistsError):
            admission.materialize_payload(destination)
        self.assertEqual((destination / "keep.txt").read_bytes(), b"peer-owned")
        self.assertFalse(list(destination.parent.glob(".retained-payload-*")))

    def test_relocated_cas_retains_exact_source_and_payload_bytes(self):
        workspace, admission = self.finish()
        _remove_tree(workspace.root)
        new_root = self.store.root.with_name("moved-cas")
        self.store.root.rename(new_root)
        moved = admission.relocated(FileSystemCAS(new_root, create=False))
        destination = self.fixture.fixture.base / "relocated-package-inputs"
        moved.materialize_payload(destination)
        self.assertTrue((destination / "app.bin").is_file())
        self.assertEqual(moved.receipt.identity, admission.receipt.identity)

    def test_source_links_retain_link_text_and_large_assets_use_streamed_cas(self):
        f = self.fixture.fixture
        (f.source / "src/link").symlink_to("main.txt")
        (f.source / "src/large.bin").write_bytes(b"\x00\xff" * (1024 * 1024))
        self.refresh_inputs(
            {"src": "source", "tools": "toolchain", "test-policy.json": "test-policy"}
        )
        original = self.store.get_bytes

        def bounded_read(reference):
            if reference.size > 1024 * 1024:
                raise AssertionError("large source payload loaded into memory")
            return original(reference)

        with patch.object(self.store, "get_bytes", side_effect=bounded_read):
            workspace, admission = self.finish()
            admission.materialize_payload(f.base / "streamed-payload")
        link = next(
            m for m in admission.run.source_bom.manifest.members if m.path == "src/link"
        )
        self.assertEqual(
            self.store.get_bytes(BlobRef(link.sha256, link.size)), b"main.txt"
        )
        self.assertEqual(link.mode, 0o120000)
        self.assertTrue((workspace.inputs / "src/link").is_symlink())

    def test_receipt_cannot_claim_generated_or_release_authority(self):
        _, admission = self.finish()
        for changes in (
            {"authority_mode": "generated"},
            {"source_authority": "generated"},
            {"conversion_advanced": True},
            {"release_authorized": True},
        ):
            wire = {**admission.receipt.to_dict(), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RetainedProjectReceipt.from_dict(wire)


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc") and shutil.which("cmake"),
    "requires native POSIX SDK fixture",
)
class RetainedProjectSdkReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        commands.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(commands.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.fixture = sdk_fixtures.RetainedProjectSdkExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_real_sdk_receipt_requires_retained_command_and_final_authority_records(
        self,
    ):
        workspace, _ = self.fixture.finish()
        store = self.fixture.sdk_fixture.fixture.store
        admission = admit_retained_project(workspace, store)
        self.assertTrue(admission.run.native_sdk_final_check_identity)
        self.assertIn(
            admission.run.native_sdk_final_check_identity.uri,
            {ref.identity for ref in admission.receipt.records},
        )
        destination = self.fixture.fixture.base / "sdk-package-inputs"
        _remove_tree(workspace.root)
        admission.materialize_payload(destination)
        self.assertTrue((destination / "app.bin").is_file())

    def test_missing_sdk_evidence_does_not_mint_admission(self):
        workspace, _ = self.fixture.finish()
        store = FileSystemCAS(self.fixture.fixture.base / "missing-evidence")
        with self.assertRaises(FileNotFoundError):
            admit_retained_project(workspace, store)
