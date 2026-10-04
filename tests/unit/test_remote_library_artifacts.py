"""Worker package custody; no generated package is imported or executed."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters.directory_artifacts import directory_export_bytes
from literate_ai.adapters.remote_execution import (
    RemoteExecutionError,
    _persist_library_artifact,
    _resolve_artifact,
    execute_remote_request,
)
from literate_ai.contracts import BlobRef, LifecycleDispatchAction, canonical_identity
from tests.support.fixtures_test_library_products import library_product
from tests.support.fixtures_test_remote_execution import identity, materialization, request, worker


class RemoteLibraryArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.artifact = self.root / "runtime" / "package"
        self.artifact.mkdir(parents=True)
        (self.artifact / "__init__.py").write_bytes(b"answer = 1\n")
        content = directory_export_bytes(self.artifact)
        product = library_product()
        self.product = replace(
            product,
            artifact_export=replace(
                product.artifact_export,
                blob=BlobRef(
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                    media_type=product.artifact_export.media_type,
                ),
            ),
        )
        self.cas = self.root / "cas"

    def retain(self, *, product=None, toolchains=None):
        return _persist_library_artifact(
            self.artifact,
            self.product if product is None else product,
            self.cas,
            (identity("5"),) if toolchains is None else toolchains,
        )

    def test_exact_package_survives_runtime_cleanup_without_commands(self) -> None:
        reference = self.retain()
        expected_bytes = directory_export_bytes(self.artifact)
        shutil.rmtree(self.artifact.parent)
        payload, manifest = _resolve_artifact(reference, self.cas)
        self.assertEqual(directory_export_bytes(payload), expected_bytes)
        self.assertEqual(manifest["library_artifact"], self.product.to_dict())
        self.assertIn("/library/", reference.uri)
        self.assertNotIn("/execution/", reference.uri)
        self.assertNotIn("argv", manifest)
        self.assertNotIn("environment", manifest)
        self.assertNotIn("entrypoints", manifest)
        self.assertFalse((payload.parent / "execution.json").exists())

    def test_metadata_and_toolchains_select_distinct_immutable_custody(self) -> None:
        first = self.retain()
        changed = replace(
            self.product,
            artifact_export=replace(
                self.product.artifact_export, component_revision=identity("6")
            ),
        )
        second = self.retain(product=changed)
        third = self.retain(toolchains=(identity("6"),))
        fourth = self.retain(
            product=replace(
                self.product,
                import_surface=replace(self.product.import_surface, package="other"),
            )
        )
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(first.identity, third.identity)
        self.assertEqual(len({first.uri, second.uri, third.uri, fourth.uri}), 4)
        for reference in (first, second, third, fourth):
            self.assertTrue(_resolve_artifact(reference, self.cas)[0].is_dir())
        self.assertEqual(self.retain(), first)

    def test_rejects_untyped_product_empty_toolchains_and_wrong_blob(self) -> None:
        for changes in (
            {"product": self.product.to_dict()},
            {"toolchains": ()},
            {"toolchains": ("sha256:" + "5" * 64,)},
            {"product": library_product()},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(RemoteExecutionError):
                    self.retain(**changes)
        self.assertFalse(self.cas.exists())

    def test_copied_byte_tamper_fails_before_publication(self) -> None:
        original_copy = shutil.copytree

        def corrupt(source, destination):
            original_copy(source, destination)
            (destination / "__init__.py").write_bytes(b"answer = 2\n")

        with patch(
            "literate_ai.adapters.remote_execution.shutil.copytree", side_effect=corrupt
        ):
            with self.assertRaisesRegex(RemoteExecutionError, "accepted export blob"):
                self.retain()
        self.assertEqual(list(self.cas.rglob("library.json")), [])
        self.assertEqual(list(self.cas.rglob(".lib-*")), [])

    def test_corrupted_existing_package_is_refused_not_overwritten(self) -> None:
        reference = self.retain()
        payload, _ = _resolve_artifact(reference, self.cas)
        (payload / "__init__.py").write_bytes(b"changed\n")
        for operation in (self.retain, lambda: _resolve_artifact(reference, self.cas)):
            with self.assertRaises(RemoteExecutionError):
                operation()
        self.assertEqual((payload / "__init__.py").read_bytes(), b"changed\n")

    @unittest.skipIf(os.name == "nt", "POSIX mode-bit semantics")
    def test_non_executable_permission_drift_is_rejected(self) -> None:
        reference = self.retain()
        payload, _ = _resolve_artifact(reference, self.cas)
        member = payload / "__init__.py"
        member.chmod(member.stat().st_mode ^ 0o040)
        with self.assertRaisesRegex(RemoteExecutionError, "accepted export blob"):
            _resolve_artifact(reference, self.cas)

    def test_metadata_tamper_and_fake_command_are_rejected(self) -> None:
        reference = self.retain()
        payload, value = _resolve_artifact(reference, self.cas)
        path = payload.parent / "library.json"
        for changed in (
            value | {"argv": ["python", "payload"]},
            value | {"toolchain_identities": [identity("6").to_dict()]},
            value | {"schema": "literate-ai/worker-artifact-execution@1"},
        ):
            with self.subTest(changed=changed):
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(RemoteExecutionError):
                    _resolve_artifact(reference, self.cas)

    def test_rehashed_false_blob_descriptor_cannot_authorize_package(self) -> None:
        reference = self.retain()
        payload, value = _resolve_artifact(reference, self.cas)
        value["library_artifact"] = library_product().to_dict()
        changed_identity = canonical_identity(value)
        changed_entry = payload.parent.parent / changed_identity.digest
        shutil.copytree(payload.parent, changed_entry)
        (changed_entry / "library.json").write_text(json.dumps(value), encoding="utf-8")
        changed_reference = replace(
            reference,
            uri=f"litai-worker-cas:{reference.identity.uri}/library/{changed_identity.uri}",
        )
        with self.assertRaisesRegex(RemoteExecutionError, "accepted export blob"):
            _resolve_artifact(changed_reference, self.cas)

    def test_locator_extensions_and_executable_downgrade_are_refused(self) -> None:
        reference = self.retain()
        for uri in (
            reference.uri + "?ignored=1",
            reference.uri + "#ignored",
            reference.uri.replace("/library/", "/execution/"),
            "litai-worker-cas:" + reference.identity.uri,
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(RemoteExecutionError):
                    _resolve_artifact(replace(reference, uri=uri), self.cas)

    def test_concurrent_verified_publisher_is_reused(self) -> None:
        def publish_winner(source, destination):
            shutil.copytree(source, destination)
            raise FileExistsError("another publisher won")

        with patch(
            "literate_ai.adapters.remote_execution.os.replace",
            side_effect=publish_winner,
        ):
            reference = self.retain()
        self.assertTrue(_resolve_artifact(reference, self.cas)[0].is_dir())
        self.assertEqual(list(self.cas.rglob(".lib-*")), [])

    def test_failed_publication_cleans_only_its_own_staging(self) -> None:
        previous = self.retain()
        with patch(
            "literate_ai.adapters.remote_execution.os.replace",
            side_effect=PermissionError("publication refused"),
        ):
            with self.assertRaises(RemoteExecutionError):
                self.retain(toolchains=(identity("6"),))
        self.assertTrue(_resolve_artifact(previous, self.cas)[0].is_dir())
        self.assertEqual(list(self.cas.rglob(".lib-*")), [])

    def test_linked_package_or_cas_root_is_refused(self) -> None:
        target = self.root / "outside"
        target.mkdir()
        try:
            self.cas.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"host cannot create symlinks: {exc}")
        with self.assertRaises(RemoteExecutionError):
            self.retain()
        self.assertEqual(list(target.iterdir()), [])
        self.cas.unlink()
        (self.artifact / "escape").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RemoteExecutionError, "links"):
            self.retain()

    def test_linked_manifest_is_refused_even_with_identical_bytes(self) -> None:
        reference = self.retain()
        payload, _ = _resolve_artifact(reference, self.cas)
        manifest = payload.parent / "library.json"
        original = payload.parent / "original.json"
        manifest.rename(original)
        try:
            manifest.symlink_to(original)
        except OSError as exc:
            self.skipTest(f"host cannot create symlinks: {exc}")
        with self.assertRaisesRegex(RemoteExecutionError, "regular file"):
            _resolve_artifact(reference, self.cas)

    def test_worker_run_refuses_library_before_any_process_or_rebuild(self) -> None:
        reference = self.retain()
        project = self.root / "project"
        project.mkdir()
        (project / "component.md").write_bytes(b"# example\n")
        selected_request = request(
            LifecycleDispatchAction.RUN, artifact_reference=reference
        )
        rebuild = Mock()
        with (
            patch("literate_ai.adapters.remote_execution.run_with_tree_kill") as run,
            patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities"
            ) as probe,
        ):
            with self.assertRaises(RemoteExecutionError) as raised:
                execute_remote_request(
                    worker(),
                    selected_request,
                    materialization(selected_request, project),
                    project_root=project,
                    cas_root=self.cas,
                    rebuild=rebuild,
                )
        self.assertEqual(
            raised.exception.code, "execution.remote_library_not_executable"
        )
        run.assert_not_called()
        rebuild.assert_not_called()
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
