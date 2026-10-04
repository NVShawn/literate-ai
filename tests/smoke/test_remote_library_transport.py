"""Library transport and public CLI contracts with an injected accepted lifecycle."""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.artifact_exports import (
    load_artifact_export,
)
from literate_ai.adapters.directory_artifacts import (
    directory_export_bytes,
)
from literate_ai.adapters.remote_execution import (
    RemoteExecutionError,
    _write_deterministic_tar_gz,
    import_remote_evidence_bundle,
    materialize_and_execute,
)
from literate_ai.adapters.source_materialization import capture_source_archive
from literate_ai.cli import build_run
from literate_ai.contracts import (
    BlobRef,
    ExecutionDispatchResult,
    ExecutionWorker,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
    canonical_identity,
    canonical_json_bytes,
)
from tests.support.fixtures_test_cli_command_worker_lifecycle import (
    _Dispatcher,
    _request,
)
from tests.support.fixtures_test_cli_rebuild import invoke
from tests.support.fixtures_test_library_products import library_product
from tests.support.fixtures_test_remote_execution import (
    identity,
    observation,
    request,
    worker,
)


class RemoteLibraryTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "component.md").write_bytes(b"# library\n")
        self.artifact = self.root / "runtime" / "package"
        self.artifact.mkdir(parents=True)
        member = self.artifact / "__init__.py"
        member.write_bytes(b"answer = 1\n")
        if os.name != "nt":
            member.chmod(0o640)
        self.content = directory_export_bytes(self.artifact)
        product = library_product()
        self.product = replace(
            product,
            artifact_export=replace(
                product.artifact_export,
                blob=BlobRef(
                    hashlib.sha256(self.content).hexdigest(),
                    len(self.content),
                    media_type=product.artifact_export.media_type,
                ),
            ),
        )
        self.accepted = {
            "passed": True,
            "artifact": str(self.artifact),
            "runtime_root": None,
            "execution_command": None,
            "library_artifact": self.product.to_dict(),
            "observed_toolchain_identities": [identity("5").uri],
        }

    def transfer(self, action=LifecycleDispatchAction.BUILD):
        dispatch = request(action)
        staging = self.root / "staging"
        staging.mkdir()
        captured = capture_source_archive(self.project, dispatch, directory=staging)
        bundle = staging / "evidence.tar.gz"
        ticket = staging / "cleanup.json"
        self.workspace = self.root / (
            dispatch.identity.digest[:24] + "-fixture-attempt"
        )

        def rebuild(**values):
            custody = values["custody"]
            self.accepted_runtime = custody.runtime_root
            artifact = custody.runtime_root / "package"
            shutil.copytree(self.artifact, artifact)
            return self.accepted | {
                "artifact": str(artifact),
                "runtime_root": str(custody.runtime_root),
            }

        with patch(
            "literate_ai.adapters.remote_execution.probe_worker_capabilities",
            return_value=observation(),
        ):
            result = materialize_and_execute(
                worker(),
                dispatch,
                captured.materialization,
                archive=captured.path,
                workspace=self.workspace,
                cas_root=self.root / "cas",
                rebuild=rebuild,
                evidence_output=bundle,
                cleanup_ticket=ticket,
            )
        return result, bundle, ticket

    def test_rehashed_bundle_cannot_bind_different_zip_and_artifact_bytes(self) -> None:
        result, bundle, _ = self.transfer()
        staging = self.root / "repack"
        staging.mkdir()
        # These members were just emitted by this fixture, not arbitrary user input.
        with tarfile.open(bundle, "r:gz") as archive:
            for member in archive.getmembers():
                path = staging / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
        (self.artifact / "__init__.py").write_bytes(b"answer = 2\n")
        changed_zip = directory_export_bytes(self.artifact)
        (staging / "package" / "library.zip").write_bytes(changed_zip)
        changed_blob = replace(
            self.product.artifact_export.blob,
            digest=hashlib.sha256(changed_zip).hexdigest(),
            size=len(changed_zip),
        )
        changed_product = replace(
            self.product,
            artifact_export=replace(self.product.artifact_export, blob=changed_blob),
        )
        manifest = replace(
            result.evidence_manifest,
            library_product=changed_product,
            files=tuple(
                replace(
                    item,
                    identity=identity_from_blob(changed_blob),
                    size=len(changed_zip),
                )
                if item.path == "package/library.zip"
                else item
                for item in result.evidence_manifest.files
            ),
        )
        (staging / "remote-evidence-manifest.json").write_bytes(
            canonical_json_bytes(manifest.to_dict())
        )
        changed_bundle = self.root / "changed.tar.gz"
        changed_identity = _write_deterministic_tar_gz(staging, changed_bundle)
        with self.assertRaises(RemoteExecutionError) as raised:
            import_remote_evidence_bundle(
                changed_bundle,
                manifest,
                expected_bundle_identity=changed_identity,
                store_root=self.root / "rejected",
            )
        self.assertEqual(raised.exception.code, "execution.remote_library_invalid")

    def test_public_remote_build_test_and_run_use_library_discriminator(self) -> None:
        self.assert_public_remote_library(
            ExecutionWorker(
                "command", ExecutionWorkerKind.COMMAND, command=("fixture-worker",)
            )
        )

    def test_public_ssh_build_test_and_run_use_library_discriminator(self) -> None:
        self.assert_public_remote_library(
            ExecutionWorker(
                "ssh",
                ExecutionWorkerKind.SSH,
                endpoint="user@host",
                workspace="~/literate-ai",
            )
        )

    def assert_public_remote_library(self, selected_worker) -> None:
        product = self.product

        class LibraryDispatcher(_Dispatcher):
            calls = []

            def dispatch(self, worker, request, *, cwd):
                result = super().dispatch(worker, request, cwd=cwd)
                # Exercise the same strict wire boundary as a command worker.
                return ExecutionDispatchResult.from_dict(
                    replace(result, library_product=product).to_dict()
                )

        selected = SimpleNamespace(
            worker=selected_worker,
            parameters=(),
            catalog_identity=canonical_identity("catalog"),
        )
        project = SimpleNamespace(
            root=self.project, definition=SimpleNamespace(source_intelligence=object())
        )
        with (
            patch.object(build_run, "_project", return_value=project),
            patch.object(build_run, "discover_project", return_value=project),
            patch.object(build_run, "select_execution_worker", return_value=selected),
            patch.object(
                build_run, "create_execution_dispatch_request", side_effect=_request
            ),
            patch.object(build_run, "CommandExecutionDispatcher", LibraryDispatcher),
            patch.object(build_run, "SshExecutionDispatcher", LibraryDispatcher),
            patch.object(build_run, "require_lifecycle_project_index", return_value={}),
        ):
            for action in ("build", "test"):
                status, document = invoke(
                    action, "components/demo", "--project", str(self.project)
                )
                self.assertEqual(status, 0, document)
                self.assertEqual(
                    document["result"]["library_artifact"], product.to_dict()
                )
                self.assertNotIn("run", document["result"])
                loaded = load_artifact_export(self.project, "demo")
                self.assertEqual(loaded.library_product, product)
                calls = len(LibraryDispatcher.calls)
                status, document = invoke(
                    "run", "components/demo", "--project", str(self.project)
                )
                self.assertNotEqual(status, 0)
                self.assertEqual(
                    document["error"]["code"], "run.library_not_executable"
                )
                self.assertEqual(len(LibraryDispatcher.calls), calls)


def identity_from_blob(blob):
    from literate_ai.contracts import ContentIdentity

    return ContentIdentity.parse_uri("sha256:" + blob.digest)


if __name__ == "__main__":
    unittest.main()
