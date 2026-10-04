"""Library package custody and public CLI behavior without model execution."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.directory_artifacts import directory_export_bytes
from literate_ai.cli import build_run
from literate_ai.contracts import (
    BlobRef,
    ExecutionWorker,
    ExecutionWorkerKind,
    canonical_identity,
)
from tests.support.fixtures_test_artifact_exports import dispatch_request
from tests.support.fixtures_test_cli_rebuild import invoke
from tests.support.fixtures_test_library_products import library_product


class LibraryExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "runtime"
        self.artifact = self.runtime / "package"
        self.worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        self.prepare_package()

    def prepare_package(self) -> None:
        self.artifact.mkdir(parents=True, exist_ok=True)
        self.artifact.joinpath("__init__.py").write_bytes(b"answer = 1\n")
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

    def test_public_build_and_test_retain_library_then_run_refuses(self) -> None:
        selected = SimpleNamespace(
            worker=self.worker,
            parameters=(),
            catalog_identity=canonical_identity({"catalog": "fixture"}),
        )
        project = SimpleNamespace(
            root=self.root, definition=SimpleNamespace(source_intelligence=object())
        )

        def request(_args, *, action, **_kwargs):
            return replace(dispatch_request(self.worker), action=action)

        for action in ("build", "test"):
            with self.subTest(action=action):
                self.prepare_package()
                accepted = {
                    "passed": True,
                    "artifact": str(self.artifact),
                    "runtime_root": str(self.runtime),
                    "execution_command": None,
                    "library_artifact": self.product.to_dict(),
                }
                with (
                    patch.object(build_run, "_project", return_value=project),
                    patch.object(build_run, "discover_project", return_value=project),
                    patch.object(
                        build_run, "select_execution_worker", return_value=selected
                    ),
                    patch.object(
                        build_run,
                        "create_execution_dispatch_request",
                        side_effect=request,
                    ) as dispatch,
                    patch.object(build_run, "_scan_coverage_gaps", return_value=None),
                    patch.object(
                        build_run, "require_lifecycle_project_index", return_value={}
                    ),
                    patch(
                        "literate_ai.cli.rebuild.rebuild_from_args",
                        return_value=accepted,
                    ),
                ):
                    status, value = invoke(
                        action, "components/demo", "--project", str(self.root)
                    )
                    self.assertEqual(status, 0, value)
                    result = value["result"]
                    self.assertEqual(result["library_artifact"], self.product.to_dict())
                    self.assertNotIn("run", result)
                    self.assertFalse(self.runtime.exists())
                    self.assertTrue(Path(result["artifact"]).is_dir())
                    calls = dispatch.call_count
                    status, value = invoke(
                        "run", "components/demo", "--project", str(self.root)
                    )
                    self.assertNotEqual(status, 0, value)
                    self.assertEqual(
                        value["error"]["code"], "run.library_not_executable"
                    )
                    self.assertEqual(dispatch.call_count, calls)


if __name__ == "__main__":
    unittest.main()
