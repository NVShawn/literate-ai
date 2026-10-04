from __future__ import annotations

import hashlib
import os
import tarfile
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai import remote_source_guard
from literate_ai.adapters.cache.filesystem import _native_filesystem_path
from literate_ai.adapters.remote_execution import (
    RemoteExecutionError,
    RemoteLifecycleCustody,
    _persist_artifact,
    _portable_result,
    _resolve_artifact,
    _write_deterministic_tar_gz,
    acknowledge_remote_evidence_cleanup,
    execute_remote_request,
    import_remote_evidence_bundle,
    load_and_import_remote_evidence_bundle,
    materialize_and_execute,
    remote_control_summary,
)
from literate_ai.adapters.source_materialization import capture_source_archive
from literate_ai.cache_directories import bind_cache_directories
from literate_ai.cli.worker import worker_from_args
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionRequirements,
    ExecutionSourceMaterialization,
    ExecutionSourceMaterializationKind,
    ExecutionWorker,
    ExecutionWorkerKind,
    HashAlgorithm,
    LifecycleDispatchAction,
    NvidiaProbeStatus,
    ObservedExecutionEnvironment,
    ObservedGpuDevice,
    RemoteEvidenceFile,
    RemoteExecutionControlResult,
    RemoteFailureDiagnostic,
    RemoteLifecycleEvidenceManifest,
    WorkerHardwareObservation,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.remote_source_guard import source_tree_entries, source_tree_identity


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def worker() -> ExecutionWorker:
    return ExecutionWorker(
        "ssh",
        ExecutionWorkerKind.SSH,
        target_profile="linux-host",
        requirements=ExecutionRequirements(os_family="linux"),
        endpoint="user@host",
        workspace="~/literate-ai",
    )


def request(
    action: LifecycleDispatchAction,
    *,
    artifact_reference=None,
    arguments: tuple[str, ...] = (),
    entrypoint: str | None = None,
) -> ExecutionDispatchRequest:
    selected = worker()
    return ExecutionDispatchRequest(
        action,
        "component://example/app",
        "components/app",
        selected.target_profile,
        ("+flavor://example/os-linux",),
        selected.identity,
        selected.requirements,
        (),
        arguments,
        identity("1"),
        identity("2"),
        identity("3"),
        identity("4"),
        identity("5"),
        identity("6"),
        identity("7"),
        artifact_reference,
        30,
        entrypoint=entrypoint,
    )


def materialization(
    dispatch_request: ExecutionDispatchRequest, project: Path
) -> ExecutionSourceMaterialization:
    return ExecutionSourceMaterialization(
        ExecutionSourceMaterializationKind.ARCHIVE,
        dispatch_request.identity,
        ContentIdentity.parse_uri(source_tree_identity(project)),
        archive_reference=ContentReference(
            "source-archive", "staged:source.tar.gz", identity("9")
        ),
    )


def observation() -> WorkerHardwareObservation:
    return WorkerHardwareObservation(
        "local-probe",
        "2026-08-12T00:00:00Z",
        "linux",
        "ubuntu",
        "24.04",
        "x86_64",
        4,
        8,
        16384,
        (
            ObservedGpuDevice(
                "nvidia",
                "NVIDIA RTX PRO 4500 Blackwell Generation",
                index=0,
                memory_mib=24564,
                compute_capability="12.0",
            ),
        ),
        NvidiaProbeStatus.OK,
    )


class RemoteExecutionTests(unittest.TestCase):
    def test_portable_result_encodes_floats_for_canonical_evidence(self) -> None:
        projected = _portable_result(
            {
                "build_cache": {
                    "hits": 1,
                    "hit_seconds": 0.0,
                    "build_seconds": 0.125,
                }
            }
        )

        self.assertEqual(
            projected,
            {
                "build_cache": {
                    "hits": 1,
                    "hit_seconds": {"encoding": "decimal-v1", "value": "0"},
                    "build_seconds": {
                        "encoding": "decimal-v1",
                        "value": "0.125",
                    },
                }
            },
        )
        canonical_json_bytes(projected)

    def test_portable_result_rejects_non_finite_floats(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(RemoteExecutionError) as raised:
                    _portable_result({"build_seconds": value})
                self.assertEqual(
                    raised.exception.code,
                    "execution.remote_result_noncanonical",
                )

    def test_worker_cas_keys_same_payload_by_its_execution_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "app"
            artifact.write_bytes(b"same-payload")
            first = _persist_artifact(
                artifact,
                {"argv": ["runtime", "--mode", "one"], "environment": {}},
                root / "cas",
                (identity("5"),),
            )
            second = _persist_artifact(
                artifact,
                {"argv": ["runtime", "--mode", "two"], "environment": {}},
                root / "cas",
                (identity("5"),),
            )

            self.assertEqual(first.identity, second.identity)
            self.assertNotEqual(first.uri, second.uri)
            first_payload, first_command = _resolve_artifact(first, root / "cas")
            self.assertEqual(first_command["argv"][-1], "one")
            self.assertEqual(
                _resolve_artifact(second, root / "cas")[1]["argv"][-1], "two"
            )
            manifest = first_payload.parent / "execution.json"
            changed = __import__("json").loads(manifest.read_text(encoding="utf-8"))
            changed["argv"][-1] = "changed"
            manifest.write_text(__import__("json").dumps(changed), encoding="utf-8")
            with self.assertRaises(RemoteExecutionError) as rejected:
                _resolve_artifact(first, root / "cas")
            self.assertEqual(
                rejected.exception.code,
                "execution.remote_artifact_manifest_changed",
            )

    def test_large_27_cell_manifest_uses_bounded_out_of_band_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            payload_identity = ContentIdentity(
                HashAlgorithm.SHA256, hashlib.sha256(b"x").hexdigest()
            )
            files = tuple(
                sorted(
                    (
                        RemoteEvidenceFile(
                            f"log/cell-{index % 27:02d}/entry-{index:05d}.json",
                            "log",
                            1,
                            payload_identity,
                        )
                        for index in range(6000)
                    ),
                    key=lambda item: item.path,
                )
            )
            observed = ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(identity("5"),),
            )
            dispatch_result = ExecutionDispatchResult(
                identity("1"),
                identity("2"),
                "27-cell-attempt",
                DispatchResultStatus.PASSED,
                observed,
                0,
                None,
                identity("3"),
            )
            manifest = RemoteLifecycleEvidenceManifest(
                dispatch_result.request_identity,
                identity("4"),
                dispatch_result.worker_identity,
                identity("5"),
                identity("6"),
                identity("7"),
                identity("8"),
                identity("9"),
                identity("a"),
                identity("b"),
                "run",
                "passed",
                dispatch_result.evidence_identity,
                canonical_identity(observed.to_dict()),
                None,
                None,
                tuple(
                    sorted(
                        (
                            ContentIdentity(
                                HashAlgorithm.SHA256,
                                hashlib.sha256(f"cell-{index}".encode()).hexdigest(),
                            )
                            for index in range(27)
                        ),
                        key=lambda item: item.uri,
                    )
                ),
                files,
                None,
                "0" * 32,
            )
            manifest_bytes = canonical_json_bytes(manifest.to_dict())
            self.assertGreater(len(manifest_bytes), 1024 * 1024)
            for item in files:
                path = staging.joinpath(*item.path.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            (staging / "remote-evidence-manifest.json").write_bytes(manifest_bytes)
            bundle = root / "remote-evidence.tar.gz"
            bundle_identity = _write_deterministic_tar_gz(staging, bundle)
            reference = ContentReference(
                "remote-lifecycle-evidence",
                "staged:remote-evidence.tar.gz",
                bundle_identity,
            )
            result_with_custody = replace(
                dispatch_result,
                evidence_manifest=manifest,
                evidence_reference=reference,
            )
            control = RemoteExecutionControlResult.from_dispatch_result(
                result_with_custody,
                manifest_size=len(manifest_bytes),
                bundle_size=bundle.stat().st_size,
                redacted_summary="27 cells passed",
            )
            control_bytes = canonical_json_bytes(control.to_dict())
            self.assertLess(len(control_bytes), 1024 * 1024)
            self.assertNotIn(b"entry-05999", control_bytes)

            imported_manifest, store_identity, imported = (
                load_and_import_remote_evidence_bundle(
                    bundle,
                    expected_manifest_identity=control.manifest_identity,
                    expected_manifest_size=control.manifest_size,
                    expected_bundle_identity=control.evidence_reference.identity,
                    expected_bundle_size=control.bundle_size,
                    store_root=root / "coordinator-cas",
                )
            )
            accepted = control.bind_imported_manifest(imported_manifest)
            self.assertEqual(accepted.status, DispatchResultStatus.PASSED)
            self.assertEqual(len(imported), 1)
            self.assertIsInstance(store_identity, ContentIdentity)

            for field, value, code in (
                (
                    "manifest_size",
                    control.manifest_size - 1,
                    "execution.remote_evidence_manifest_size_mismatch",
                ),
                (
                    "bundle_size",
                    control.bundle_size + 1,
                    "execution.remote_evidence_bundle_size_mismatch",
                ),
            ):
                malformed = replace(control, **{field: value})
                with self.assertRaises(RemoteExecutionError) as raised:
                    load_and_import_remote_evidence_bundle(
                        bundle,
                        expected_manifest_identity=malformed.manifest_identity,
                        expected_manifest_size=malformed.manifest_size,
                        expected_bundle_identity=malformed.evidence_reference.identity,
                        expected_bundle_size=malformed.bundle_size,
                        store_root=root / "rejected-cas",
                    )
                self.assertEqual(raised.exception.code, code)

    def test_control_summary_is_redacted(self) -> None:
        failure = RemoteFailureDiagnostic(
            "lifecycle.failed",
            "password=<redacted> at <private-path>",
            stderr="diagnostic detail",
        )
        result = ExecutionDispatchResult(
            identity("1"),
            identity("2"),
            "task",
            DispatchResultStatus.FAILED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(identity("5"),),
            ),
            1,
            None,
            identity("3"),
            "",
            "token=hunter2 at /Users/worker/private/build.log",
            failure.identity.uri,
        )
        summary = remote_control_summary(result)
        self.assertNotIn("hunter2", summary)
        self.assertNotIn("/Users/worker", summary)
        self.assertIn("<redacted>", summary)
        self.assertIn("<private-path>", summary)

    def test_receiver_uses_manifest_when_host_cannot_expose_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            executable = project / "run"
            executable.write_bytes(b"#!/usr/bin/env python3\n")
            executable.chmod(0o755)
            accepted_entries = tuple(source_tree_entries(project))
            dispatch_request = request(LifecycleDispatchAction.BUILD)
            envelope = materialization(dispatch_request, project)
            executable.chmod(0o644)
            artifact = root / "artifact"
            artifact.write_bytes(b"artifact")
            rebuild = Mock(
                return_value={
                    "artifact": str(artifact),
                    "execution_command": {
                        "argv": [__import__("sys").executable, str(artifact)],
                        "environment": {},
                    },
                    "observed_toolchain_identities": [identity("5").uri],
                    "runtime_root": None,
                }
            )

            def windows_verify(source_root, entries):
                return remote_source_guard.verify_materialized_source(
                    source_root, entries, executable_mode_supported=False
                )

            with (
                patch(
                    "literate_ai.adapters.remote_execution.verify_materialized_source",
                    wraps=windows_verify,
                ),
                patch(
                    "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                    return_value=observation(),
                ),
            ):
                result = execute_remote_request(
                    worker(),
                    dispatch_request,
                    envelope,
                    project_root=project,
                    cas_root=root / "cas",
                    rebuild=rebuild,
                    accepted_source_entries=accepted_entries,
                )
            self.assertEqual(result.status, DispatchResultStatus.PASSED)

    def test_worker_rebuild_uses_explicit_directory_custody_not_environment(
        self,
    ) -> None:
        selected = replace(
            request(LifecycleDispatchAction.BUILD),
            accepted_source_only=True,
            accepted_source_provider_id="inherited-session",
            accepted_source_provider_identity=identity("a"),
        )
        args = Mock(
            worker_command="execute",
            worker_file="worker.json",
            request="request.json",
            materialization="materialization.json",
            archive="source.tar.gz",
            workspace="workspace",
            cas_root="cas",
            accepted_source_cache="accepted-source-cache.tar.gz",
            evidence_output=None,
            cleanup_ticket=None,
        )
        values = [
            worker().to_dict(),
            selected.to_dict(),
            {"fixture": "materialization"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            attempt = root / "short root with spaces"
            project.mkdir()
            attempt.mkdir()
            directories = bind_cache_directories(
                project,
                build_dir=attempt / "build",
                obj_dir=attempt / "objects",
            )
            custody = RemoteLifecycleCustody(
                attempt,
                directories,
                attempt / "runtime",
                attempt / "candidate receipt.json",
            )
            with (
                patch("literate_ai.cli.worker._read_contract", side_effect=values),
                patch(
                    "literate_ai.contracts.ExecutionSourceMaterialization.from_dict",
                    return_value=Mock(),
                ),
                patch(
                    "literate_ai.adapters.remote_execution.materialize_and_execute",
                    return_value=Mock(
                        to_dict=lambda: {"ok": True}, status=Mock(value="passed")
                    ),
                ) as materialize,
                patch("literate_ai.cli.rebuild.rebuild_from_args") as rebuild,
            ):
                worker_from_args(args)
                callback = materialize.call_args.kwargs["rebuild"]
                callback(
                    component_path="components/app",
                    project_root=project,
                    custody=custody,
                    flavor_selectors=(),
                    target_profile="windows-host",
                    model_selector=None,
                    accepted_source_only=True,
                    accepted_source_provider_id="inherited-session",
                    accepted_source_provider_identity=identity("a"),
                    jobs=2,
                )

        rebuild.assert_called_once()
        namespace = rebuild.call_args.args[0]
        self.assertEqual(namespace.jobs, 2)
        self.assertFalse(hasattr(namespace, "cache_environment"))
        self.assertIs(rebuild.call_args.kwargs["cache_directories"], directories)
        self.assertEqual(
            rebuild.call_args.kwargs["cache_directories"].identity,
            custody.cache_directories.identity,
        )
        self.assertEqual(
            namespace.accepted_source_provider_id,
            "inherited-session",
        )
        self.assertEqual(
            namespace.accepted_source_provider_identity,
            identity("a"),
        )

    def test_parallel_build_persists_artifact_and_run_uses_exact_worker_cas(
        self,
    ) -> None:
        from literate_ai.contracts.capabilities import DependencyKind
        from tests.support import fixtures_test_standard_project_lifecycle as lifecycle

        lock = lifecycle._diamond_lock(dependency_kind=DependencyKind.BUILD)
        execution, requests = lifecycle._prepared_execution(lock)
        nodes = lifecycle._prepared_nodes(execution, requests)
        names = lifecycle._names(lock)
        rendezvous = threading.Barrier(2, timeout=10)

        class ParallelPorts(lifecycle.LifecyclePorts):
            def build(self, plan, provider_artifacts):
                if self.names[plan.component_revision.uri] in {"pricing", "reporting"}:
                    rendezvous.wait()
                return super().build(plan, provider_artifacts)

        ports = ParallelPorts(execution, names)

        def rebuild_graph(**values):
            result = lifecycle._service(ports).execute(
                execution,
                component_lock=lock,
                invalidation=lifecycle._decision(
                    execution, names, "money", tuple(names.values())
                ),
                prepared_nodes=nodes,
                max_parallelism=values["jobs"],
            )
            self.assertTrue(result.successful)
            events = {event: index for index, event in enumerate(ports.events)}
            for child in ("pricing", "reporting"):
                self.assertLess(events[("accept", "money")], events[("intent", child)])
                self.assertLess(
                    events[("accept", child)], events[("intent", "invoice-cli")]
                )
            return rebuilt_artifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            runtime = root / "runtime"
            runtime.mkdir()
            artifact = runtime / "app.py"
            artifact.write_text(
                "import sys; print('remote:' + sys.argv[1])\n", encoding="utf-8"
            )
            build_request = replace(request(LifecycleDispatchAction.BUILD), jobs=2)
            rebuilt_artifact = {
                "artifact": str(artifact),
                "execution_command": {
                    "argv": [__import__("sys").executable, str(artifact)],
                    "environment": {},
                },
                "observed_toolchain_identities": [identity("5").uri],
                "runtime_root": None,
            }
            rebuild = Mock(side_effect=rebuild_graph)
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                built = execute_remote_request(
                    worker(),
                    build_request,
                    materialization(build_request, project),
                    project_root=project,
                    cas_root=root / "cas",
                    rebuild=rebuild,
                )
            self.assertEqual(built.status, DispatchResultStatus.PASSED)
            rebuild_args = rebuild.call_args.kwargs
            self.assertEqual(rebuild_args["jobs"], 2)
            custody = rebuild_args["custody"]
            self.assertEqual(
                custody.runtime_root,
                project.resolve().parent / "lifecycle-runtime",
            )
            self.assertEqual(
                custody.candidate_receipt,
                project.resolve().parent / "candidate-receipt.json",
            )
            self.assertEqual(
                built.artifact_reference.uri.split(":", 1)[0], "litai-worker-cas"
            )

            run_request = request(
                LifecycleDispatchAction.RUN,
                artifact_reference=built.artifact_reference,
                arguments=("hello",),
            )
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                ran = execute_remote_request(
                    worker(),
                    run_request,
                    materialization(run_request, project),
                    project_root=project,
                    cas_root=root / "cas",
                    rebuild=rebuild,
                )
            self.assertEqual(ran.status, DispatchResultStatus.PASSED)
            self.assertEqual(ran.stdout, "remote:hello\n")

    def test_remote_multi_entrypoint_run_selects_named_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            artifact = root / "runtime" / "outputs"
            artifact.mkdir(parents=True)
            primary = artifact / "api.py"
            collector = artifact / "collector.py"
            primary.write_text("print('api')\n", encoding="utf-8")
            collector.write_text("print('collector')\n", encoding="utf-8")
            python = __import__("sys").executable
            entrypoints = (
                {
                    "schema": "literate-ai/artifact-entrypoint-command@1",
                    "name": "api",
                    "kind": "portable-application",
                    "deployment_unit": "api-service",
                    "argv": [python, str(primary)],
                    "environment": {},
                },
                {
                    "schema": "literate-ai/artifact-entrypoint-command@1",
                    "name": "collector",
                    "kind": "portable-application",
                    "deployment_unit": "collector-service",
                    "argv": [python, str(collector)],
                    "environment": {},
                },
            )
            build_request = request(LifecycleDispatchAction.BUILD)
            rebuild = Mock(
                return_value={
                    "artifact": str(artifact),
                    "execution_command": {
                        "argv": entrypoints[0]["argv"],
                        "environment": {},
                    },
                    "execution_entrypoints": entrypoints,
                    "observed_toolchain_identities": [identity("5").uri],
                    "runtime_root": None,
                }
            )
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                built = execute_remote_request(
                    worker(),
                    build_request,
                    materialization(build_request, project),
                    project_root=project,
                    cas_root=root / "cas",
                    rebuild=rebuild,
                )
                run_request = request(
                    LifecycleDispatchAction.RUN,
                    artifact_reference=built.artifact_reference,
                    entrypoint="collector",
                )
                ran = execute_remote_request(
                    worker(),
                    run_request,
                    materialization(run_request, project),
                    project_root=project,
                    cas_root=root / "cas",
                    rebuild=rebuild,
                )

            self.assertEqual(ran.status, DispatchResultStatus.PASSED)
            self.assertEqual(ran.stdout, "collector\n")

    def test_build_scans_generated_sources_into_dispatch_coverage_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            runtime = root / "runtime"
            (project).mkdir()
            (runtime / "sources").mkdir(parents=True)
            (runtime / "sources" / "server.py").write_text(
                "ROUTES = {'/v1/chat/completions': None}\n",
                encoding="utf-8",
            )
            artifact = runtime / "app.bin"
            artifact.write_bytes(b"artifact")
            dispatch_request = request(LifecycleDispatchAction.BUILD)
            rebuild = Mock(
                return_value={
                    "artifact": str(artifact),
                    "execution_command": {
                        "argv": [__import__("sys").executable, str(artifact)],
                        "environment": {},
                    },
                    "observed_toolchain_identities": [identity("5").uri],
                    "runtime_root": str(runtime),
                }
            )
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                result = execute_remote_request(
                    worker(),
                    dispatch_request,
                    materialization(dispatch_request, project),
                    project_root=project,
                    cas_root=root / "cas",
                    rebuild=rebuild,
                )
            self.assertEqual(result.status, DispatchResultStatus.PASSED)
            self.assertIsNotNone(result.coverage_gaps)
            assert result.coverage_gaps is not None
            gaps = result.coverage_gaps["gaps"]
            self.assertEqual(gaps[0]["reason"], "none_bound_handler")
            self.assertEqual(gaps[0]["gate"], "fail_closed")
            self.assertFalse(runtime.exists())

    def test_materialize_and_execute_restores_the_staged_accepted_source_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            cache_root = project / "generated" / "accepted-source-cache"
            entry_dir = cache_root / "entries" / "sha256" / "ab"
            entry_dir.mkdir(parents=True)
            (entry_dir / "cd.json").write_text("{}", encoding="utf-8")
            (cache_root / "format.json").write_text("{}", encoding="utf-8")

            build_request = replace(
                request(LifecycleDispatchAction.BUILD), accepted_source_only=True
            )
            with patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}):
                captured = capture_source_archive(
                    project, build_request, directory=staging
                )
            self.assertIsNotNone(captured.accepted_source_cache_path)

            artifact = root / "artifact"
            artifact.write_bytes(b"artifact")
            rebuild = Mock(
                return_value={
                    "artifact": str(artifact),
                    "execution_command": {
                        "argv": [__import__("sys").executable, str(artifact)],
                        "environment": {},
                    },
                    "observed_toolchain_identities": [identity("5").uri],
                    "runtime_root": None,
                }
            )
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                result = materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=root / "workspace",
                    cas_root=root / "cas",
                    rebuild=rebuild,
                    accepted_source_cache_archive=captured.accepted_source_cache_path,
                )
            self.assertEqual(result.status, DispatchResultStatus.PASSED)

    def test_twelve_cache_memberships_use_short_attempt_root_and_are_cleaned(
        self,
    ) -> None:
        # Use the native namespace for both fixture creation and cleanup; the
        # coordinator path remains deliberately long and the receiver stays short.
        with tempfile.TemporaryDirectory(
            dir=_native_filesystem_path(Path(tempfile.gettempdir()))
        ) as directory:
            root = Path(directory)
            project = root / ("coordinator project with spaces " + ("p" * 80))
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            cache_root = project / "generated" / "accepted-source-cache"
            (cache_root / "format.json").parent.mkdir(parents=True)
            (cache_root / "format.json").write_text("{}", encoding="utf-8")
            expected: dict[str, bytes] = {}
            for index in range(12):
                key = hashlib.sha256(f"key-{index}".encode()).hexdigest()
                entry = hashlib.sha256(f"entry-{index}".encode()).hexdigest()
                relative = f"keys/sha256/{key}/{entry}.json"
                content = f'{{"membership":{index}}}'.encode()
                path = cache_root.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True)
                path.write_bytes(content)
                expected[relative] = content

            build_request = replace(
                request(LifecycleDispatchAction.BUILD), accepted_source_only=True
            )
            with patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}):
                captured = capture_source_archive(
                    project, build_request, directory=staging
                )
            assert captured.accepted_source_cache_path is not None
            with tarfile.open(captured.path, mode="r:gz") as archive:
                source_members = tuple(member.name for member in archive.getmembers())
            self.assertFalse(
                any(
                    member.startswith("literate-ai/generated/accepted-source-cache/")
                    for member in source_members
                )
            )
            artifact = root / "artifact"
            artifact.write_bytes(b"artifact")
            observed_build_dirs: list[Path] = []
            observed_custody_identities: list[ContentIdentity] = []

            def rebuild(**values):
                custody = values["custody"]
                observed_custody_identities.append(custody.identity)
                build_dir = custody.cache_directories.build_dir
                observed_build_dirs.append(build_dir)
                self.assertFalse(build_dir.is_relative_to(values["project_root"]))
                restored = build_dir / "accepted-source-cache"
                actual = {
                    item.relative_to(restored).as_posix(): item.read_bytes()
                    for item in restored.rglob("*.json")
                    if item.name != "format.json"
                }
                self.assertEqual(actual, expected)
                self.assertNotIn("\\", next(iter(actual)))
                self.assertTrue(
                    all(len(str(item)) < 260 for item in restored.rglob("*"))
                )
                self.assertTrue(
                    custody.cache_directories.obj_dir.is_relative_to(
                        custody.attempt_root
                    )
                )
                self.assertTrue(
                    custody.runtime_root.is_relative_to(custody.attempt_root)
                )
                self.assertTrue(
                    custody.candidate_receipt.is_relative_to(custody.attempt_root)
                )
                long_lookup = (
                    values["project_root"]
                    / "generated"
                    / "accepted-source-cache"
                    / next(iter(expected))
                )
                self.assertGreater(len(str(long_lookup)), 260)
                self.assertFalse(long_lookup.exists())
                return {
                    "artifact": str(artifact),
                    "execution_command": {
                        "argv": [__import__("sys").executable, str(artifact)],
                        "environment": {},
                    },
                    "observed_toolchain_identities": [identity("5").uri],
                    "runtime_root": None,
                }

            workspace = root / (build_request.identity.digest[:24] + "-" + ("w" * 100))
            evidence = root / "remote-evidence.tar.gz"
            cleanup_ticket = root / "cleanup-ticket.json"
            original_write_bytes = Path.write_bytes

            def reject_legacy_windows_length(path: Path, content: bytes) -> int:
                if len(str(path)) >= 260:
                    raise OSError("simulated Windows legacy path rejection")
                return original_write_bytes(path, content)

            with (
                patch.object(Path, "write_bytes", reject_legacy_windows_length),
                patch(
                    "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                    return_value=observation(),
                ),
            ):
                result = materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=workspace,
                    cas_root=root / "cas",
                    rebuild=rebuild,
                    accepted_source_cache_archive=captured.accepted_source_cache_path,
                    evidence_output=evidence,
                    cleanup_ticket=cleanup_ticket,
                )

            self.assertEqual(result.status, DispatchResultStatus.PASSED)
            self.assertIsNotNone(result.evidence_manifest)
            self.assertTrue(evidence.is_file())
            self.assertEqual(len(observed_build_dirs), 1)
            self.assertEqual(len(observed_custody_identities), 1)
            self.assertTrue(observed_build_dirs[0].exists())
            self.assertTrue(workspace.exists())
            assert result.evidence_manifest is not None
            assert result.evidence_reference is not None
            acknowledge_remote_evidence_cleanup(
                cleanup_ticket,
                manifest_identity=result.evidence_manifest.identity,
                bundle_identity=result.evidence_reference.identity,
                acknowledgement_root=root / "acknowledgements",
            )
            self.assertFalse(observed_build_dirs[0].exists())
            self.assertFalse(workspace.exists())

    def test_short_attempt_custody_is_cleaned_when_rebuild_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            cache_root = project / "generated" / "accepted-source-cache"
            key = cache_root / "keys" / "sha256" / ("a" * 64) / f"{'b' * 64}.json"
            key.parent.mkdir(parents=True)
            key.write_text("{}", encoding="utf-8")
            (cache_root / "format.json").write_text("{}", encoding="utf-8")
            build_request = replace(
                request(LifecycleDispatchAction.BUILD), accepted_source_only=True
            )
            with patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}):
                captured = capture_source_archive(
                    project, build_request, directory=staging
                )
            assert captured.accepted_source_cache_path is not None
            observed_attempts: list[Path] = []

            def fail_rebuild(**values):
                observed_attempts.append(values["custody"].attempt_root)
                raise RuntimeError("expected lifecycle failure")

            workspace = root / "remote workspace"
            with self.assertRaisesRegex(RuntimeError, "expected lifecycle failure"):
                materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=workspace,
                    cas_root=root / "cas",
                    rebuild=fail_rebuild,
                    accepted_source_cache_archive=captured.accepted_source_cache_path,
                )

            self.assertEqual(len(observed_attempts), 1)
            self.assertFalse(observed_attempts[0].exists())
            self.assertFalse(workspace.exists())

    def test_materialize_and_execute_fails_closed_on_cache_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            cache_root = project / "generated" / "accepted-source-cache"
            cache_root.mkdir(parents=True)
            (cache_root / "format.json").write_text("{}", encoding="utf-8")

            build_request = replace(
                request(LifecycleDispatchAction.BUILD), accepted_source_only=True
            )
            with patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}):
                captured = capture_source_archive(
                    project, build_request, directory=staging
                )
            self.assertIsNotNone(captured.accepted_source_cache_path)
            captured.accepted_source_cache_path.write_bytes(b"tampered")

            with self.assertRaises(RemoteExecutionError) as raised:
                materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=root / "workspace",
                    cas_root=root / "cas",
                    rebuild=Mock(),
                    accepted_source_cache_archive=captured.accepted_source_cache_path,
                )
            self.assertEqual(
                raised.exception.code,
                "execution.remote_accepted_source_cache_mismatch",
            )

    def test_materialize_and_execute_fails_closed_when_cache_archive_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            cache_root = project / "generated" / "accepted-source-cache"
            cache_root.mkdir(parents=True)
            (cache_root / "format.json").write_text("{}", encoding="utf-8")

            build_request = replace(
                request(LifecycleDispatchAction.BUILD), accepted_source_only=True
            )
            with patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}):
                captured = capture_source_archive(
                    project, build_request, directory=staging
                )
            self.assertIsNotNone(
                captured.materialization.accepted_source_cache_reference
            )

            with self.assertRaises(RemoteExecutionError) as raised:
                materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=root / "workspace",
                    cas_root=root / "cas",
                    rebuild=Mock(),
                )
            self.assertEqual(
                raised.exception.code,
                "execution.remote_accepted_source_cache_mismatch",
            )

    def test_request_and_materialized_tree_mismatches_fail_before_lifecycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            dispatch_request = request(LifecycleDispatchAction.BUILD)
            envelope = materialization(dispatch_request, project)
            (project / "component.md").write_text("# changed\n", encoding="utf-8")
            with self.assertRaises(RemoteExecutionError) as raised:
                execute_remote_request(
                    worker(),
                    dispatch_request,
                    envelope,
                    project_root=project,
                    cas_root=project / "cas",
                    rebuild=Mock(),
                )
            self.assertEqual(raised.exception.code, "execution.remote_source_mismatch")

    def test_remote_evidence_is_verified_imported_and_cleaned_after_ack(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            build_request = request(LifecycleDispatchAction.BUILD)
            captured = capture_source_archive(project, build_request, directory=staging)
            observed_attempts: list[Path] = []

            def rebuild(**values):
                custody = values["custody"]
                observed_attempts.append(custody.attempt_root)
                custody.runtime_root.mkdir()
                artifact = custody.runtime_root / "artifact"
                (artifact / "app-inspect").mkdir(parents=True)
                (artifact / "app-inspect" / "payload.bin").write_bytes(b"inspect")
                (artifact / "app").mkdir()
                (artifact / "app" / "payload.bin").write_bytes(b"primary")
                custody.cache_directories.obj_dir.mkdir(parents=True)
                (custody.cache_directories.obj_dir / "app.o").write_bytes(b"object")
                custody.cache_directories.build_dir.mkdir(parents=True, exist_ok=True)
                (custody.cache_directories.build_dir / "package.json").write_text(
                    "{}", encoding="utf-8"
                )
                custody.candidate_receipt.write_text("{}", encoding="utf-8")
                return {
                    "artifact": str(artifact),
                    "execution_command": {
                        "argv": [
                            __import__("sys").executable,
                            str(artifact / "app" / "payload.bin"),
                        ],
                        "environment": {},
                    },
                    "observed_toolchain_identities": [identity("5").uri],
                    "lifecycle_result_identity": identity("b").uri,
                    "receipt_identity": identity("c").uri,
                    "build_cache": {
                        "hits": 1,
                        "hit_seconds": 0.0,
                        "build_seconds": 0.125,
                    },
                    "runtime_root": str(custody.runtime_root),
                }

            workspace = root / (build_request.identity.digest[:24] + "-fixture-attempt")
            bundle = staging / "remote-evidence.tar.gz"
            ticket = staging / "cleanup-ticket.json"
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                result = materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=workspace,
                    cas_root=root / "worker-cas",
                    rebuild=rebuild,
                    evidence_output=bundle,
                    cleanup_ticket=ticket,
                )

            self.assertEqual(result.status, DispatchResultStatus.PASSED)
            self.assertIsNotNone(result.evidence_manifest)
            self.assertIsNotNone(result.evidence_reference)
            assert result.evidence_manifest is not None
            assert result.evidence_reference is not None
            self.assertEqual(
                result.evidence_manifest.stage_identities,
                (identity("5"), identity("b"), identity("c")),
            )
            self.assertTrue(workspace.exists())
            store_identity, imported = import_remote_evidence_bundle(
                bundle,
                result.evidence_manifest,
                expected_bundle_identity=result.evidence_reference.identity,
                store_root=root / "coordinator-cas",
            )
            repeated_store_identity, repeated_imported = import_remote_evidence_bundle(
                bundle,
                result.evidence_manifest,
                expected_bundle_identity=result.evidence_reference.identity,
                store_root=root / "coordinator-cas",
            )
            self.assertEqual(repeated_store_identity, store_identity)
            self.assertEqual(repeated_imported, imported)
            self.assertEqual(
                set(imported),
                {item.identity for item in result.evidence_manifest.files},
            )

            acknowledgement = acknowledge_remote_evidence_cleanup(
                ticket,
                manifest_identity=result.evidence_manifest.identity,
                bundle_identity=result.evidence_reference.identity,
                acknowledgement_root=root / "acknowledgements",
            )
            repeated = acknowledge_remote_evidence_cleanup(
                ticket,
                manifest_identity=result.evidence_manifest.identity,
                bundle_identity=result.evidence_reference.identity,
                acknowledgement_root=root / "acknowledgements",
            )
            self.assertEqual(repeated, acknowledgement)
            with self.assertRaises(RemoteExecutionError) as raised:
                acknowledge_remote_evidence_cleanup(
                    ticket,
                    manifest_identity=result.evidence_manifest.identity,
                    bundle_identity=identity("f"),
                    acknowledgement_root=root / "acknowledgements",
                )
            self.assertEqual(
                raised.exception.code,
                "execution.remote_cleanup_acknowledgement_mismatch",
            )
            self.assertFalse(workspace.exists())
            self.assertTrue(bundle.is_file())
            self.assertEqual(observed_attempts, [workspace.resolve()])

    def test_remote_evidence_import_rejects_tampered_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            build_request = request(LifecycleDispatchAction.BUILD)
            captured = capture_source_archive(project, build_request, directory=staging)

            def rebuild(**values):
                custody = values["custody"]
                custody.runtime_root.mkdir()
                artifact = custody.runtime_root / "app.bin"
                artifact.write_bytes(b"binary")
                return {
                    "artifact": str(artifact),
                    "execution_command": {
                        "argv": [__import__("sys").executable, str(artifact)],
                        "environment": {},
                    },
                    "observed_toolchain_identities": [identity("5").uri],
                    "runtime_root": str(custody.runtime_root),
                }

            bundle = staging / "remote-evidence.tar.gz"
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                result = materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=root
                    / (build_request.identity.digest[:24] + "-tamper-attempt"),
                    cas_root=root / "worker-cas",
                    rebuild=rebuild,
                    evidence_output=bundle,
                    cleanup_ticket=staging / "cleanup-ticket.json",
                )
            assert result.evidence_manifest is not None
            assert result.evidence_reference is not None

            tampered = staging / "tampered.tar.gz"
            tampered.write_bytes(bundle.read_bytes() + b"tampered")
            with self.assertRaises(RemoteExecutionError) as raised:
                import_remote_evidence_bundle(
                    tampered,
                    result.evidence_manifest,
                    expected_bundle_identity=result.evidence_reference.identity,
                    store_root=root / "coordinator-cas",
                )
            self.assertEqual(
                raised.exception.code, "execution.remote_evidence_bundle_mismatch"
            )

            extracted = staging / "extracted"
            extracted.mkdir()
            with tarfile.open(bundle, mode="r:gz") as archive:
                archive.extractall(extracted, filter="data")
            artifact_path = next(extracted.glob("artifact/*"))
            artifact_path.unlink()
            missing = staging / "missing.tar.gz"
            missing_identity = _write_deterministic_tar_gz(extracted, missing)
            with self.assertRaises(RemoteExecutionError) as raised:
                import_remote_evidence_bundle(
                    missing,
                    result.evidence_manifest,
                    expected_bundle_identity=missing_identity,
                    store_root=root / "coordinator-cas",
                )
            self.assertEqual(
                raised.exception.code, "execution.remote_evidence_file_mismatch"
            )

            duplicate = staging / "duplicate.tar.gz"
            duplicate_source = next(
                item for item in extracted.rglob("*") if item.is_file()
            )
            with tarfile.open(duplicate, mode="w:gz") as archive:
                for item in sorted(extracted.rglob("*")):
                    if item.is_file():
                        archive.add(
                            item, arcname=item.relative_to(extracted).as_posix()
                        )
                archive.add(
                    duplicate_source,
                    arcname=duplicate_source.relative_to(extracted).as_posix(),
                )
            duplicate_identity = ContentIdentity(
                HashAlgorithm.SHA256,
                hashlib.sha256(duplicate.read_bytes()).hexdigest(),
            )
            with self.assertRaises(RemoteExecutionError) as raised:
                import_remote_evidence_bundle(
                    duplicate,
                    result.evidence_manifest,
                    expected_bundle_identity=duplicate_identity,
                    store_root=root / "coordinator-cas",
                )
            self.assertEqual(
                raised.exception.code, "execution.remote_evidence_bundle_invalid"
            )

    def test_preflight_failure_diagnostic_survives_until_ack_and_is_redacted(
        self,
    ) -> None:
        class NestedPreflightError(RuntimeError):
            code = "source-index.preflight-command-failed"

        class PreflightError(RuntimeError):
            code = "source-index.preflight-failed"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            build_request = request(LifecycleDispatchAction.BUILD)
            captured = capture_source_archive(project, build_request, directory=staging)

            def fail_rebuild(**_values):
                try:
                    nested = NestedPreflightError(
                        r"token=hunter2 at C:\Users\worker\private\source.log"
                    )
                    nested.stdout = "index started in /Users/worker/private/project"
                    nested.stderr = "credential=worker-secret"
                    raise nested
                except NestedPreflightError as exc:
                    raise PreflightError(
                        "password=secret at /Users/worker/private/project"
                    ) from exc

            workspace = root / (build_request.identity.digest[:24] + "-failure-attempt")
            bundle = staging / "failure-evidence.tar.gz"
            ticket = staging / "cleanup-ticket.json"
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                result = materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=workspace,
                    cas_root=root / "worker-cas",
                    rebuild=fail_rebuild,
                    evidence_output=bundle,
                    cleanup_ticket=ticket,
                )

            self.assertEqual(result.status, DispatchResultStatus.FAILED)
            assert result.evidence_manifest is not None
            assert result.evidence_reference is not None
            failure = result.evidence_manifest.failure
            assert failure is not None
            self.assertEqual(failure.code, "source-index.preflight-failed")
            self.assertEqual(
                failure.nested_code, "source-index.preflight-command-failed"
            )
            public = str(failure.to_dict())
            self.assertNotIn("hunter2", public)
            self.assertNotIn("password=secret", public)
            self.assertNotIn("Users", public)
            self.assertIn("<redacted>", public)
            self.assertIn("<private-path>", public)
            self.assertIn("index started", failure.stdout)
            self.assertNotIn("/Users/worker", failure.stdout)
            self.assertNotIn("worker-secret", failure.stderr)
            self.assertTrue(workspace.exists())
            import_remote_evidence_bundle(
                bundle,
                result.evidence_manifest,
                expected_bundle_identity=result.evidence_reference.identity,
                store_root=root / "coordinator-cas",
            )
            acknowledge_remote_evidence_cleanup(
                ticket,
                manifest_identity=result.evidence_manifest.identity,
                bundle_identity=result.evidence_reference.identity,
                acknowledgement_root=root / "acknowledgements",
            )
            self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
