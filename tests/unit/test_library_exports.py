"""Library package custody and public CLI behavior without model execution."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.artifact_exports import (
    EXPORT_RECORD,
    ArtifactExportError,
    load_artifact_export,
    record_artifact_export,
)
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

    def retain(self, **changes):
        arguments = {
            "artifact": self.artifact,
            "execution_command": {},
            "target_profile": "host",
            "worker": self.worker,
            "dispatch_request": dispatch_request(self.worker),
            "library_product": self.product,
        }
        return record_artifact_export(
            self.root, "components/demo", **(arguments | changes)
        )

    def test_package_survives_runtime_cleanup_without_a_command(self) -> None:
        export = self.retain()
        shutil.rmtree(self.runtime)
        loaded = load_artifact_export(self.root, "demo")
        self.assertEqual(loaded, export)
        self.assertEqual(loaded.library_product, self.product)
        self.assertEqual(loaded.argv, ())
        self.assertEqual(
            loaded.artifact.joinpath("__init__.py").read_bytes(), b"answer = 1\n"
        )
        self.assertNotIn("execution_identity", loaded.to_dict())

    def test_repeated_copy_keeps_previous_package_custody(self) -> None:
        first = self.retain()
        second = self.retain()
        self.assertNotEqual(first.artifact, second.artifact)
        self.assertTrue(first.artifact.is_dir())
        self.assertEqual(load_artifact_export(self.root, "demo"), second)

    def test_changed_source_cannot_replace_previous_valid_export(self) -> None:
        previous = self.retain()
        record = previous.artifact.parent.parent / EXPORT_RECORD
        before = record.read_bytes()
        self.artifact.joinpath("__init__.py").write_bytes(b"answer = 2\n")
        with self.assertRaisesRegex(ArtifactExportError, "accepted export blob"):
            self.retain()
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(load_artifact_export(self.root, "demo"), previous)

    def test_failed_copy_or_publication_preserves_the_previous_record(self) -> None:
        previous = self.retain()
        record = previous.artifact.parent.parent / EXPORT_RECORD
        before = record.read_bytes()
        real_copy = shutil.copytree

        def corrupt_copy(source, destination):
            real_copy(source, destination)
            destination.joinpath("__init__.py").write_bytes(b"corrupted\n")

        for name, effect in (
            ("shutil.copytree", corrupt_copy),
            ("os.replace", OSError("refused")),
        ):
            with self.subTest(name=name):
                with patch(
                    "literate_ai.adapters.artifact_exports." + name, side_effect=effect
                ):
                    with self.assertRaises(ArtifactExportError):
                        self.retain()
                self.assertEqual(record.read_bytes(), before)
                self.assertEqual(load_artifact_export(self.root, "demo"), previous)
                self.assertEqual(
                    list(record.parent.glob("lib-*")), [previous.artifact.parent]
                )

    def test_load_rejects_copied_bytes_and_rehashed_blob_descriptor_drift(self) -> None:
        export = self.retain()
        record = export.artifact.parent.parent / EXPORT_RECORD
        wrong_product = replace(
            self.product,
            artifact_export=replace(
                self.product.artifact_export,
                blob=replace(self.product.artifact_export.blob, digest="b" * 64),
            ),
        )
        forged = replace(export, library_product=wrong_product)
        record.write_bytes((json.dumps(forged.to_dict()) + "\n").encode())
        with self.assertRaisesRegex(ArtifactExportError, "accepted export blob"):
            load_artifact_export(self.root, "demo")
        record.write_bytes((json.dumps(export.to_dict()) + "\n").encode())
        export.artifact.joinpath("__init__.py").write_bytes(b"changed\n")
        with self.assertRaises(ArtifactExportError) as caught:
            load_artifact_export(self.root, "demo")
        self.assertEqual(caught.exception.code, "artifact_export.changed")

    def test_library_rejects_fake_commands_and_unbound_metadata(self) -> None:
        with self.assertRaises(ArtifactExportError):
            self.retain(execution_command={"argv": ["fake"]})
        export = self.retain()
        record = export.artifact.parent.parent / EXPORT_RECORD
        value = export.to_dict()
        value["library_artifact"]["import_surface"]["package"] = "changed"
        record.write_bytes((json.dumps(value) + "\n").encode())
        with self.assertRaisesRegex(ArtifactExportError, "metadata changed"):
            load_artifact_export(self.root, "demo")

    def test_library_rejects_linked_output_custody(self) -> None:
        from literate_ai.adapters.artifact_exports import export_root

        outside = self.root / "outside"
        outside.mkdir()
        parent = export_root(self.root)
        parent.parent.mkdir(parents=True, exist_ok=True)
        try:
            parent.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(ArtifactExportError):
            self.retain()
        self.assertEqual(list(outside.iterdir()), [])

    def test_library_discriminator_and_identity_are_required_together(self) -> None:
        export = self.retain()
        record = export.artifact.parent.parent / EXPORT_RECORD
        for missing in ("library_artifact", "library_identity"):
            with self.subTest(missing=missing):
                value = export.to_dict()
                del value[missing]
                record.write_bytes((json.dumps(value) + "\n").encode())
                with self.assertRaises(ArtifactExportError):
                    load_artifact_export(self.root, "demo")

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
