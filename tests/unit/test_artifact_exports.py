from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.artifact_exports import (
    ENTRYPOINT_COMMAND_SCHEMA,
    EXPORT_SCHEMA,
    ArtifactExportError,
    load_artifact_export,
    record_artifact_export,
    record_remote_artifact_export,
)
from literate_ai.contracts import (
    ContentReference,
    ExecutionDispatchRequest,
    ExecutionWorker,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
    canonical_identity,
)


def dispatch_request(worker: ExecutionWorker) -> ExecutionDispatchRequest:
    identities = tuple(canonical_identity({"value": index}) for index in range(7))
    return ExecutionDispatchRequest(
        LifecycleDispatchAction.BUILD,
        "component://example/demo",
        "components/demo",
        worker.target_profile,
        (),
        worker.identity,
        worker.requirements,
        (),
        (),
        *identities,
        None,
        60,
    )


class ArtifactExportTests(unittest.TestCase):
    def test_export_binds_exact_target_profile_and_worker(self) -> None:
        worker = ExecutionWorker(
            "local", ExecutionWorkerKind.LOCAL, target_profile="macos-host"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "runtime" / "app"
            artifact.parent.mkdir()
            artifact.write_bytes(b"executable")

            recorded = record_artifact_export(
                root,
                "components/demo",
                artifact=artifact,
                execution_command={"argv": [str(artifact)], "environment": {}},
                target_profile="macos-host",
                worker=worker,
                dispatch_request=dispatch_request(worker),
            )
            loaded = load_artifact_export(root, "demo")

        self.assertEqual(recorded, loaded)
        self.assertEqual(recorded.to_dict()["schema"], EXPORT_SCHEMA)
        self.assertEqual(recorded.component, "components/demo")
        self.assertEqual(recorded.target_profile, "macos-host")
        self.assertEqual(recorded.worker_id, "local")
        self.assertEqual(recorded.worker_identity, worker.identity)
        self.assertNotEqual(recorded.artifact, artifact)
        self.assertEqual(recorded.locality, "local")
        self.assertEqual(recorded.artifact_reference.uri, recorded.artifact.as_uri())
        self.assertNotIn("entrypoints", recorded.to_dict())
        self.assertNotIn("default_entrypoint", recorded.to_dict())
        self.assertNotIn("model_selector", recorded.to_dict())
        self.assertNotIn("accepted_source_only", recorded.to_dict())
        self.assertNotIn("library_artifact", recorded.to_dict())
        self.assertNotIn("library_identity", recorded.to_dict())

    def test_export_retains_explicit_model_selector_for_run_authority(self) -> None:
        worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "runtime" / "app"
            artifact.parent.mkdir()
            artifact.write_bytes(b"executable")
            request = dispatch_request(worker)
            request = __import__("dataclasses").replace(
                request, model_selector="gpt-5.6-sol"
            )

            recorded = record_artifact_export(
                root,
                "components/demo",
                artifact=artifact,
                execution_command={"argv": [str(artifact)], "environment": {}},
                target_profile="host",
                worker=worker,
                dispatch_request=request,
            )
            loaded = load_artifact_export(root, "demo")

        self.assertEqual(recorded.model_selector, "gpt-5.6-sol")
        self.assertEqual(loaded, recorded)
        self.assertEqual(loaded.to_dict()["model_selector"], "gpt-5.6-sol")

    def test_export_retains_accepted_source_mode_for_run_authority(self) -> None:
        worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "runtime" / "app"
            artifact.parent.mkdir()
            artifact.write_bytes(b"executable")
            request = __import__("dataclasses").replace(
                dispatch_request(worker), accepted_source_only=True
            )

            recorded = record_artifact_export(
                root,
                "components/demo",
                artifact=artifact,
                execution_command={"argv": [str(artifact)], "environment": {}},
                target_profile="host",
                worker=worker,
                dispatch_request=request,
            )
            loaded = load_artifact_export(root, "demo")

            record = loaded.artifact.parent / "execution.json"
            value = __import__("json").loads(record.read_text(encoding="utf-8"))
            value["accepted_source_only"] = "true"
            record.write_text(__import__("json").dumps(value), encoding="utf-8")
            with self.assertRaises(ArtifactExportError) as invalid:
                load_artifact_export(root, "demo")

        self.assertTrue(recorded.accepted_source_only)
        self.assertEqual(loaded, recorded)
        self.assertIs(loaded.to_dict()["accepted_source_only"], True)
        self.assertEqual(invalid.exception.code, "artifact_export.invalid")

    def test_multi_entrypoint_export_retains_shared_root_and_every_command(
        self,
    ) -> None:
        worker = ExecutionWorker(
            "local", ExecutionWorkerKind.LOCAL, target_profile="macos-host"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "runtime" / "outputs"
            artifact.mkdir(parents=True)
            (artifact / "api.py").write_text("api\n", encoding="utf-8")
            (artifact / "worker.py").write_text("worker\n", encoding="utf-8")
            commands = (
                {
                    "schema": ENTRYPOINT_COMMAND_SCHEMA,
                    "name": "api",
                    "kind": "portable-application",
                    "deployment_unit": "api-service",
                    "argv": ["python3", str(artifact / "api.py")],
                    "environment": {"APP_ROOT": str(artifact)},
                },
                {
                    "schema": ENTRYPOINT_COMMAND_SCHEMA,
                    "name": "collector",
                    "kind": "portable-application",
                    "deployment_unit": "collector-service",
                    "argv": ["python3", str(artifact / "worker.py")],
                    "environment": {"APP_ROOT": str(artifact)},
                },
            )

            recorded = record_artifact_export(
                root,
                "components/demo",
                artifact=artifact,
                execution_command={
                    "argv": commands[0]["argv"],
                    "environment": commands[0]["environment"],
                },
                execution_entrypoints=commands,
                target_profile="macos-host",
                worker=worker,
                dispatch_request=dispatch_request(worker),
            )
            loaded = load_artifact_export(root, "demo")

            self.assertEqual(loaded, recorded)
            self.assertTrue(loaded.artifact.is_dir())
            self.assertEqual(
                tuple(item.name for item in loaded.entrypoints),
                ("api", "collector"),
            )
            self.assertEqual(loaded.default_entrypoint, "api")
            self.assertIn(
                str(loaded.artifact / "worker.py"), loaded.entrypoints[1].argv
            )
            self.assertNotIn(str(artifact), " ".join(loaded.entrypoints[1].argv))

            record = loaded.artifact.parent / "execution.json"
            value = __import__("json").loads(record.read_text(encoding="utf-8"))
            value["entrypoints"][1]["argv"].append("--changed")
            record.write_text(__import__("json").dumps(value), encoding="utf-8")
            with self.assertRaises(ArtifactExportError) as changed:
                load_artifact_export(root, "demo")
            self.assertEqual(changed.exception.code, "artifact_export.invalid")

    def test_remote_export_records_only_durable_reference_and_authority(self) -> None:
        worker = ExecutionWorker(
            "fleet",
            ExecutionWorkerKind.COMMAND,
            command=("dispatcher",),
        )
        request = dispatch_request(worker)
        reference = ContentReference(
            "artifact-export",
            "cas:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            canonical_identity({"artifact": "demo"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = record_remote_artifact_export(
                root,
                "components/demo",
                artifact_reference=reference,
                target_profile="host",
                worker=worker,
                dispatch_request=request,
            )
            loaded = load_artifact_export(root, "demo")

        self.assertEqual(loaded, recorded)
        self.assertEqual(loaded.component, "components/demo")
        self.assertEqual(loaded.locality, "worker")
        self.assertIsNone(loaded.artifact)
        self.assertEqual(loaded.argv, ())
        self.assertEqual(loaded.artifact_reference, reference)
        self.assertEqual(loaded.authority_identity, request.authority_identity)


if __name__ == "__main__":
    unittest.main()
