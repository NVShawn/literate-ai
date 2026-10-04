"""Worker package custody; no generated package is imported or executed."""

from __future__ import annotations

import hashlib
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
    execute_remote_request,
)
from literate_ai.contracts import BlobRef, LifecycleDispatchAction
from tests.support.fixtures_test_library_products import library_product
from tests.support.fixtures_test_remote_execution import (
    identity,
    materialization,
    request,
    worker,
)


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
