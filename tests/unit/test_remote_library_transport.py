"""Library transport and public CLI contracts with an injected accepted lifecycle."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.artifact_exports import (
    ArtifactExportError,
    load_artifact_export,
    record_remote_artifact_export,
)
from literate_ai.adapters.directory_artifacts import (
    directory_export_bytes,
    require_transported_library_package,
)
from literate_ai.adapters.remote_execution import (
    RemoteExecutionError,
    _resolve_artifact,
    _write_deterministic_tar_gz,
    acknowledge_remote_evidence_cleanup,
    execute_remote_request,
    import_remote_evidence_bundle,
    load_and_import_remote_evidence_bundle,
    materialize_and_execute,
)
from literate_ai.adapters.source_materialization import capture_source_archive
from literate_ai.cli import build_run
from literate_ai.contracts import (
    BlobRef,
    ContentReference,
    ContractValidationError,
    ExecutionDispatchResult,
    ExecutionWorker,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
    RemoteExecutionControlResult,
    RemoteLifecycleEvidenceManifest,
    canonical_identity,
    canonical_json_bytes,
)
from tests.unit.test_cli_command_worker_lifecycle import _Dispatcher, _request
from tests.unit.test_cli_rebuild import invoke
from tests.unit.test_library_products import library_product
from tests.unit.test_remote_execution import (
    identity,
    materialization,
    observation,
    request,
    worker,
)
from tests.unit.test_wire_contract_versions import _v2_schemas


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

    def test_worker_build_and_test_return_typed_products_without_commands(self) -> None:
        for action in (LifecycleDispatchAction.BUILD, LifecycleDispatchAction.TEST):
            with self.subTest(action=action):
                dispatch = request(action)
                with patch(
                    "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                    return_value=observation(),
                ):
                    result = execute_remote_request(
                        worker(),
                        dispatch,
                        materialization(dispatch, self.project),
                        project_root=self.project,
                        cas_root=self.root / "cas",
                        rebuild=Mock(return_value=self.accepted),
                    )
                self.assertEqual(result.library_product, self.product)
                self.assertEqual(
                    ExecutionDispatchResult.from_dict(result.to_dict()), result
                )
                payload, manifest = _resolve_artifact(
                    result.artifact_reference, self.root / "cas"
                )
                self.assertEqual(directory_export_bytes(payload), self.content)
                self.assertNotIn("argv", manifest)
                with self.assertRaises(ContractValidationError):
                    replace(result, library_product=None)
                changed = replace(
                    self.product,
                    import_surface=replace(
                        self.product.import_surface, package="other"
                    ),
                )
                with self.assertRaises(ContractValidationError):
                    replace(result, library_product=changed)

    def test_failed_or_command_bearing_library_is_not_published(self) -> None:
        dispatch = request(LifecycleDispatchAction.BUILD)
        for changes in (
            {"passed": False},
            {"execution_command": {"argv": ["false"]}},
            {"execution_entrypoints": [{}]},
            {"library_artifact": None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(RemoteExecutionError):
                    execute_remote_request(
                        worker(),
                        dispatch,
                        materialization(dispatch, self.project),
                        project_root=self.project,
                        cas_root=self.root / "cas",
                        rebuild=Mock(return_value=self.accepted | changes),
                    )
        self.assertFalse((self.root / "cas").exists())

    def test_evidence_and_control_round_trip_preserve_exact_sealed_package(
        self,
    ) -> None:
        result, bundle, ticket = self.transfer(LifecycleDispatchAction.TEST)
        manifest = result.evidence_manifest
        self.assertEqual(manifest.library_product, self.product)
        self.assertEqual(
            RemoteLifecycleEvidenceManifest.from_dict(manifest.to_dict()), manifest
        )
        self.assertEqual(ExecutionDispatchResult.from_dict(result.to_dict()), result)
        schemas = _v2_schemas()
        schemas.validate(result.SCHEMA, result.to_dict())
        schemas.validate(manifest.SCHEMA, manifest.to_dict())
        control = RemoteExecutionControlResult.from_dispatch_result(
            result,
            manifest_size=len(canonical_json_bytes(manifest.to_dict())),
            bundle_size=bundle.stat().st_size,
            redacted_summary="library passed",
        )
        self.assertNotIn("library_artifact", control.to_dict())
        control = RemoteExecutionControlResult.from_dict(control.to_dict())
        imported_manifest, _, imported = load_and_import_remote_evidence_bundle(
            bundle,
            expected_manifest_identity=control.manifest_identity,
            expected_manifest_size=control.manifest_size,
            expected_bundle_identity=control.evidence_reference.identity,
            expected_bundle_size=control.bundle_size,
            store_root=self.root / "coordinator",
        )
        rebound = control.bind_imported_manifest(imported_manifest)
        self.assertEqual(rebound.library_product, self.product)
        blob = self.product.artifact_export.blob
        package = (
            self.root
            / "coordinator"
            / "blobs"
            / "sha256"
            / blob.digest[:2]
            / blob.digest
        )
        self.assertEqual(package.read_bytes(), self.content)
        self.assertIn(identity_from_blob(blob), imported)
        acknowledge_remote_evidence_cleanup(
            ticket,
            manifest_identity=manifest.identity,
            bundle_identity=result.evidence_reference.identity,
            acknowledgement_root=self.root / "acks",
        )
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.accepted_runtime.exists())
        self.assertEqual(package.read_bytes(), self.content)
        self.assertTrue(
            _resolve_artifact(result.artifact_reference, self.root / "cas")[0].is_dir()
        )

    def test_import_metadata_cannot_be_changed_or_dropped_under_valid_control(
        self,
    ) -> None:
        result, bundle, _ = self.transfer()
        manifest = result.evidence_manifest
        control = RemoteExecutionControlResult.from_dispatch_result(
            result,
            manifest_size=len(canonical_json_bytes(manifest.to_dict())),
            bundle_size=bundle.stat().st_size,
            redacted_summary="passed",
        )
        changed = replace(
            self.product,
            import_surface=replace(self.product.import_surface, package="other"),
        )
        for product in (changed, None):
            with self.subTest(product=product):
                with self.assertRaises(ContractValidationError):
                    replace(result, library_product=product)
                changed_manifest = replace(manifest, library_product=product)
                with self.assertRaises(ContractValidationError):
                    control.bind_imported_manifest(changed_manifest)
        document = result.to_dict() | {"library_artifact": None}
        with self.assertRaises(ContractValidationError):
            ExecutionDispatchResult.from_dict(document)
        for changed_manifest in (
            {
                "files": tuple(
                    item
                    for item in manifest.files
                    if item.path != "package/library.zip"
                )
            },
            {"artifact_is_directory": False},
            {"action": "run"},
        ):
            with self.assertRaises(ContractValidationError):
                replace(manifest, **changed_manifest)

    def test_sealed_archive_cannot_disagree_with_transported_file_evidence(
        self,
    ) -> None:
        package = self.root / "library.zip"
        package.write_bytes(self.content)
        member = self.artifact / "__init__.py"
        member.write_bytes(b"answer = 2\n")
        with self.assertRaisesRegex(ValueError, "member differs"):
            require_transported_library_package(self.product, package, self.artifact)
        member.write_bytes(b"answer = 1\n")
        (self.artifact / "extra.py").write_bytes(b"extra\n")
        with self.assertRaisesRegex(ValueError, "members differ"):
            require_transported_library_package(self.product, package, self.artifact)

    def test_transport_checks_verified_mode_evidence_not_host_mode_projection(
        self,
    ) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            member = zipfile.ZipInfo("__init__.py", date_time=(1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(member, b"answer = 1\n")
        content = stream.getvalue()
        product = replace(
            self.product,
            artifact_export=replace(
                self.product.artifact_export,
                blob=replace(
                    self.product.artifact_export.blob,
                    digest=hashlib.sha256(content).hexdigest(),
                    size=len(content),
                ),
            ),
        )
        package = self.root / "library.zip"
        package.write_bytes(content)
        require_transported_library_package(
            product, package, self.artifact, executable_by_path={"__init__.py": True}
        )
        with self.assertRaises(ValueError):
            require_transported_library_package(
                product,
                package,
                self.artifact,
                executable_by_path={"__init__.py": False},
            )

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

    def test_remote_publication_failure_preserves_prior_library_record(self) -> None:
        selected = SimpleNamespace(
            worker=ExecutionWorker(
                "command", ExecutionWorkerKind.COMMAND, command=("fixture-worker",)
            ),
            parameters=(),
        )
        dispatch = _request(
            object(),
            component="components/demo",
            selected=selected,
            action=LifecycleDispatchAction.BUILD,
        )
        arguments = dict(
            artifact_reference=ContentReference(
                "artifact-export", "cas:fixture", identity("a")
            ),
            target_profile="host",
            worker=selected.worker,
            dispatch_request=dispatch,
            library_product=self.product,
        )
        previous = record_remote_artifact_export(
            self.project, "components/demo", **arguments
        )
        with patch(
            "literate_ai.adapters.artifact_exports.os.replace",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(ArtifactExportError):
                record_remote_artifact_export(
                    self.project, "components/demo", **arguments
                )
        self.assertEqual(load_artifact_export(self.project, "demo"), previous)
        self.assertEqual(list(self.project.rglob(".record-*")), [])

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
