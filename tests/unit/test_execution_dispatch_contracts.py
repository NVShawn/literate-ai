from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionRequirements,
    ExecutionSourceMaterialization,
    ExecutionSourceMaterializationKind,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
    ExecutionWorkerParameter,
    GpuRequirement,
    HashAlgorithm,
    LifecycleDispatchAction,
    ObservedExecutionEnvironment,
    RemoteEvidenceCustodyReceipt,
    RemoteEvidenceFile,
    RemoteExecutionControlResult,
    RemoteLifecycleEvidenceManifest,
    canonical_json_bytes,
    resolve_worker_parameters,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def artifact_reference(character: str = "7") -> ContentReference:
    return ContentReference(
        "artifact-export",
        f"cas:sha256:{character * 64}",
        identity(character),
    )


def source_archive_reference(character: str = "8") -> ContentReference:
    return ContentReference(
        "source-archive",
        "staged:source.tar.gz",
        identity(character),
    )


def workers() -> ExecutionWorkerCatalog:
    return ExecutionWorkerCatalog(
        (
            ExecutionWorker(
                "fleet",
                ExecutionWorkerKind.COMMAND,
                target_profile="linux-host",
                requirements=ExecutionRequirements(
                    os_family="linux",
                    minimum_cpu_cores=16,
                    minimum_memory_mib=32768,
                ),
                parameters=(
                    ExecutionWorkerParameter(
                        "queue", ("batch", "interactive"), default="batch"
                    ),
                ),
                command=("mac", "dispatch", "submit", "{request_file}"),
                environment=(
                    ExecutionWorkerEnvironment("MAC_TOKEN", "LITAI_MAC_TOKEN"),
                ),
            ),
            ExecutionWorker("local", ExecutionWorkerKind.LOCAL),
            ExecutionWorker(
                "ubuntu",
                ExecutionWorkerKind.SSH,
                target_profile="linux-host",
                requirements=ExecutionRequirements(
                    os_family="linux", cpu_architecture="x86_64"
                ),
                endpoint="user@ubuntu.example.invalid",
                workspace="~/literate-ai",
            ),
        )
    )


class ExecutionDispatchContractTests(unittest.TestCase):
    def test_source_materialization_is_a_closed_identity_bound_choice(self) -> None:
        git = ExecutionSourceMaterialization(
            ExecutionSourceMaterializationKind.GIT,
            identity("1"),
            identity("2"),
            repository_url="https://example.invalid/project.git",
            revision="a" * 40,
        )
        archive = ExecutionSourceMaterialization(
            ExecutionSourceMaterializationKind.ARCHIVE,
            identity("1"),
            identity("2"),
            archive_reference=source_archive_reference(),
        )

        self.assertEqual(ExecutionSourceMaterialization.from_dict(git.to_dict()), git)
        self.assertEqual(
            ExecutionSourceMaterialization.from_dict(archive.to_dict()), archive
        )
        self.assertNotEqual(git.identity, archive.identity)
        catalog = SchemaCatalog()
        catalog.validate(git.SCHEMA, git.to_dict())
        catalog.validate(archive.SCHEMA, archive.to_dict())

    def test_source_materialization_rejects_ambiguous_or_credentialed_authority(
        self,
    ) -> None:
        invalid = (
            {
                "kind": ExecutionSourceMaterializationKind.GIT,
                "repository_url": "https://token@example.invalid/project.git",
                "revision": "a" * 40,
                "archive_reference": None,
            },
            {
                "kind": ExecutionSourceMaterializationKind.GIT,
                "repository_url": "https://example.invalid/project.git",
                "revision": "main",
                "archive_reference": None,
            },
            {
                "kind": ExecutionSourceMaterializationKind.ARCHIVE,
                "repository_url": "https://example.invalid/project.git",
                "revision": None,
                "archive_reference": source_archive_reference(),
            },
            {
                "kind": ExecutionSourceMaterializationKind.ARCHIVE,
                "repository_url": None,
                "revision": None,
                "archive_reference": ContentReference(
                    "source-archive",
                    "file:///tmp/source.tar.gz",
                    identity("8"),
                ),
            },
        )
        for fields in invalid:
            with (
                self.subTest(fields=fields),
                self.assertRaises(ContractValidationError),
            ):
                ExecutionSourceMaterialization(
                    fields["kind"],
                    identity("1"),
                    identity("2"),
                    repository_url=fields["repository_url"],
                    revision=fields["revision"],
                    archive_reference=fields["archive_reference"],
                )

    def setUp(self) -> None:
        self.schemas = SchemaCatalog()

    def test_worker_catalog_round_trips_and_selects_only_exact_ids(self) -> None:
        catalog = workers()

        self.assertEqual(
            ExecutionWorkerCatalog.from_dict(catalog.to_dict()),
            catalog,
        )
        self.assertEqual(catalog.worker("ubuntu").kind, ExecutionWorkerKind.SSH)
        self.assertEqual(catalog.worker("ubuntu").target_profile, "linux-host")
        self.assertEqual(catalog.worker("ubuntu").requirements.os_family, "linux")
        with self.assertRaises(ContractValidationError):
            catalog.worker("linux-*")
        with self.assertRaises(ContractValidationError) as raised:
            catalog.worker("linux")

        self.assertIn("unknown execution worker", str(raised.exception))
        self.assertFalse(hasattr(catalog, "match"))
        self.assertFalse(hasattr(catalog, "rank"))
        self.assertFalse(hasattr(catalog, "allocate"))

    def test_ssh_worker_transport_defaults_to_ssh_and_is_configurable_per_worker(
        self,
    ) -> None:
        # A private fleet may route some workers through a drop-in SSH-compatible
        # wrapper (VPN/Tailscale-aware) instead of plain OpenSSH, while other
        # workers keep using ssh directly -- this must be per-worker data, not a
        # framework constant (see issue #65's Windows remote-restore findings).
        default = ExecutionWorker(
            "ubuntu",
            ExecutionWorkerKind.SSH,
            endpoint="user@ubuntu.example.invalid",
            workspace="~/literate-ai",
        )
        self.assertEqual(default.transport, "ssh")

        wrapped = ExecutionWorker(
            "windows",
            ExecutionWorkerKind.SSH,
            endpoint="user@windows.example.invalid",
            workspace="~/literate-ai",
            transport="s",
        )
        self.assertEqual(wrapped.transport, "s")
        self.assertEqual(ExecutionWorker.from_dict(wrapped.to_dict()), wrapped)
        self.schemas.validate(ExecutionWorker.SCHEMA, wrapped.to_dict())

    def test_worker_slots_default_to_one_and_are_identity_bound(self) -> None:
        worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        legacy = worker.to_dict()
        legacy.pop("slots")
        self.assertEqual(ExecutionWorker.from_dict(legacy).slots, 1)

        parallel = ExecutionWorker("local", ExecutionWorkerKind.LOCAL, slots=4)
        self.assertEqual(ExecutionWorker.from_dict(parallel.to_dict()), parallel)
        self.assertNotEqual(parallel.identity, worker.identity)
        self.schemas.validate(ExecutionWorker.SCHEMA, parallel.to_dict())
        for invalid in (0, 257, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ContractValidationError):
                    ExecutionWorker("local", ExecutionWorkerKind.LOCAL, slots=invalid)

    def test_non_ssh_workers_cannot_configure_a_transport(self) -> None:
        for kind, kwargs in (
            (ExecutionWorkerKind.LOCAL, {}),
            (ExecutionWorkerKind.COMMAND, {"command": ("dispatcher", "run")}),
        ):
            with self.subTest(kind=kind):
                with self.assertRaises(ContractValidationError):
                    ExecutionWorker("worker", kind, transport="s", **kwargs)

    def test_transport_must_be_a_portable_identifier(self) -> None:
        for invalid in ("", "s p a c e", "../etc/passwd", "-s", "S"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ContractValidationError):
                    ExecutionWorker(
                        "worker",
                        ExecutionWorkerKind.SSH,
                        endpoint="user@host.example.invalid",
                        workspace="~/literate-ai",
                        transport=invalid,
                    )

    def test_ssh_worker_lifecycle_executable_is_identity_bound_and_ssh_only(
        self,
    ) -> None:
        worker = ExecutionWorker(
            "ubuntu",
            ExecutionWorkerKind.SSH,
            endpoint="user@host.example.invalid",
            workspace="~/literate-ai",
            lifecycle_executable="/opt/literate-ai/verified/bin/litai",
        )
        restored = ExecutionWorker.from_dict(worker.to_dict())
        self.assertEqual(restored, worker)
        self.assertEqual(
            restored.lifecycle_executable, "/opt/literate-ai/verified/bin/litai"
        )

        for kind, kwargs in (
            (ExecutionWorkerKind.LOCAL, {}),
            (ExecutionWorkerKind.COMMAND, {"command": ("dispatcher", "run")}),
        ):
            with self.subTest(kind=kind):
                with self.assertRaises(ContractValidationError):
                    ExecutionWorker(
                        "worker",
                        kind,
                        lifecycle_executable="/opt/literate-ai/bin/litai",
                        **kwargs,
                    )

        with self.assertRaises(ContractValidationError):
            ExecutionWorker(
                "unsafe",
                ExecutionWorkerKind.SSH,
                endpoint="user@host.example.invalid",
                workspace="~/literate-ai",
                lifecycle_executable="bad\x00path",
            )

    def test_command_worker_accepts_only_stdin_or_one_whole_request_file_argument(
        self,
    ) -> None:
        stdin = ExecutionWorker(
            "stdin-dispatch", ExecutionWorkerKind.COMMAND, command=("dispatcher", "run")
        )
        request_file = workers().worker("fleet")

        self.assertEqual(stdin.command, ("dispatcher", "run"))
        self.assertIn("{request_file}", request_file.command)
        for command in (
            ("dispatcher", "--request={request_file}"),
            ("dispatcher", "{hardware.cpu}"),
            ("{request_file}",),
            ("dispatcher", "{request_file}", "{request_file}"),
        ):
            with self.subTest(command=command):
                with self.assertRaises(ContractValidationError):
                    ExecutionWorker(
                        "unsafe", ExecutionWorkerKind.COMMAND, command=command
                    )

    def test_worker_parameters_are_bounded_without_fleet_selection(self) -> None:
        worker = workers().worker("fleet")

        self.assertEqual(
            resolve_worker_parameters(worker, ()),
            (("queue", "batch"),),
            (),
        )
        self.assertEqual(
            resolve_worker_parameters(worker, (("queue", "interactive"),)),
            (("queue", "interactive"),),
        )
        with self.assertRaises(ContractValidationError):
            resolve_worker_parameters(worker, (("gpu", "h100"),))
        with self.assertRaises(ContractValidationError):
            resolve_worker_parameters(worker, (("queue", "$(touch-pwned)"),))

    def test_hardware_requirements_are_data_and_round_trip(self) -> None:
        requirements = ExecutionRequirements(
            os_family="linux",
            os_version="24.04",
            cpu_architecture="x86_64",
            minimum_cpu_cores=32,
            minimum_memory_mib=131072,
            gpu=GpuRequirement(
                vendor="nvidia",
                model="h100",
                minimum_count=4,
                minimum_memory_mib=81920,
                capabilities=("cuda",),
            ),
        )

        self.assertEqual(
            ExecutionRequirements.from_dict(requirements.to_dict()), requirements
        )
        self.assertFalse(hasattr(requirements, "select_worker"))
        self.assertFalse(hasattr(requirements, "provision"))

    def test_unset_gpu_qualifier_has_explicit_nullable_placeholders(self) -> None:
        requirements = ExecutionRequirements(os_family="linux")

        self.assertEqual(
            requirements.to_dict()["gpu"],
            {
                "schema": GpuRequirement.SCHEMA,
                "vendor": None,
                "model": None,
                "minimum_count": None,
                "minimum_memory_mib": None,
                "capabilities": None,
            },
        )
        cpu_only = ObservedExecutionEnvironment("linux", "24.04", "x86_64", 4, 8192)
        self.assertTrue(cpu_only.satisfies(requirements))

        request = ExecutionDispatchRequest(
            LifecycleDispatchAction.BUILD,
            "component://example/service",
            "components/service",
            "host",
            (),
            identity("1"),
            requirements,
            (),
            (),
            identity("2"),
            identity("3"),
            identity("4"),
            identity("5"),
            identity("6"),
            identity("7"),
            identity("8"),
            None,
            300,
        )
        self.assertIsNone(
            request.to_dict()["requirements"]["gpu"]["minimum_memory_mib"]
        )
        self.assertIsNone(request.to_dict()["requirements"]["gpu"]["capabilities"])

    def test_dispatch_request_binds_authority_and_round_trips(self) -> None:
        worker = workers().worker("fleet")
        request = ExecutionDispatchRequest(
            LifecycleDispatchAction.BUILD,
            "component://example/service",
            "components/service",
            worker.target_profile,
            ("+flavor://literate-ai/os-linux",),
            worker.identity,
            worker.requirements,
            (("queue", "batch"),),
            (),
            identity("1"),
            identity("2"),
            identity("3"),
            identity("4"),
            identity("5"),
            identity("6"),
            identity("7"),
            None,
            3600,
            verbose=True,
            model_selector="pipeline-model",
        )

        self.assertEqual(
            ExecutionDispatchRequest.from_dict(request.to_dict()),
            request,
        )
        self.assertEqual(
            request.identity,
            ExecutionDispatchRequest.from_dict(request.to_dict()).identity,
        )
        self.assertTrue(request.to_dict()["verbose"])
        self.assertEqual(request.to_dict()["model_selector"], "pipeline-model")
        self.assertNotIn("entrypoint", request.to_dict())
        self.assertEqual(request.target_profile, worker.target_profile)
        self.assertEqual(request.flavor_selectors, ("+flavor://literate-ai/os-linux",))
        quiet = ExecutionDispatchRequest.from_dict(
            {**request.to_dict(), "verbose": False}
        )
        self.assertEqual(request.authority_identity, quiet.authority_identity)
        self.assertNotEqual(request.identity, quiet.identity)
        self.assertNotIn("jobs", request.to_dict())
        self.assertEqual(request.jobs, 1)
        for jobs in (2, 256):
            parallel = replace(request, jobs=jobs)
            self.schemas.validate(parallel.SCHEMA, parallel.to_dict())
            self.assertEqual(
                ExecutionDispatchRequest.from_dict(parallel.to_dict()), parallel
            )
            self.assertEqual(parallel.to_dict()["jobs"], jobs)
            self.assertNotEqual(request.identity, parallel.identity)
            self.assertEqual(request.authority_identity, parallel.authority_identity)
        for jobs in (0, -1, 257, True, False, 1.5, "2", None):
            with self.subTest(jobs=jobs):
                with self.assertRaises(ContractValidationError):
                    replace(request, jobs=jobs)
                with self.assertRaises(ContractValidationError):
                    ExecutionDispatchRequest.from_dict(
                        {**request.to_dict(), "jobs": jobs}
                    )
        another_model = ExecutionDispatchRequest.from_dict(
            {**request.to_dict(), "model_selector": "another-model"}
        )
        self.assertNotEqual(
            request.authority_identity, another_model.authority_identity
        )
        another_target = ExecutionDispatchRequest.from_dict(
            {
                **request.to_dict(),
                "target_profile": "other",
                "flavor_selectors": ["+flavor://literate-ai/os-windows"],
            }
        )
        self.assertNotEqual(
            request.authority_identity, another_target.authority_identity
        )
        selected_entrypoint = replace(
            request,
            action=LifecycleDispatchAction.RUN,
            artifact_reference=artifact_reference(),
            entrypoint="collector",
        )
        self.assertEqual(
            ExecutionDispatchRequest.from_dict(selected_entrypoint.to_dict()),
            selected_entrypoint,
        )
        self.assertEqual(
            selected_entrypoint.authority_identity, request.authority_identity
        )
        self.assertNotEqual(selected_entrypoint.identity, request.identity)
        self.schemas.validate(selected_entrypoint.SCHEMA, selected_entrypoint.to_dict())
        with self.assertRaises(ContractValidationError):
            replace(request, entrypoint="collector")
        for component_path in ("/components/service", "components/../service", "a\\b"):
            with (
                self.subTest(component_path=component_path),
                self.assertRaises(ContractValidationError),
            ):
                ExecutionDispatchRequest.from_dict(
                    {**request.to_dict(), "component_path": component_path}
                )

    def test_dispatch_request_accepted_source_only_defaults_and_binds_authority(
        self,
    ) -> None:
        worker = workers().worker("fleet")
        base_args = (
            LifecycleDispatchAction.BUILD,
            "component://example/service",
            "components/service",
            worker.target_profile,
            ("+flavor://literate-ai/os-linux",),
            worker.identity,
            worker.requirements,
            (("queue", "batch"),),
            (),
            identity("1"),
            identity("2"),
            identity("3"),
            identity("4"),
            identity("5"),
            identity("6"),
            identity("7"),
            None,
            3600,
        )
        default_request = ExecutionDispatchRequest(*base_args)
        self.assertFalse(default_request.accepted_source_only)
        self.assertFalse(default_request.to_dict()["accepted_source_only"])
        self.assertIsNone(default_request.accepted_source_provider_identity)
        self.assertNotIn("accepted_source_provider_identity", default_request.to_dict())

        legacy_wire = default_request.to_dict()
        del legacy_wire["accepted_source_only"]
        self.assertEqual(
            ExecutionDispatchRequest.from_dict(legacy_wire), default_request
        )

        accepted_source_request = ExecutionDispatchRequest(
            *base_args,
            accepted_source_only=True,
            accepted_source_provider_id="inherited-session",
            accepted_source_provider_identity=identity("a"),
        )
        self.assertEqual(
            ExecutionDispatchRequest.from_dict(accepted_source_request.to_dict()),
            accepted_source_request,
        )
        self.assertNotEqual(
            default_request.authority_identity,
            accepted_source_request.authority_identity,
        )
        self.assertNotEqual(default_request.identity, accepted_source_request.identity)
        with self.assertRaises(ContractValidationError):
            ExecutionDispatchRequest(
                *base_args,
                accepted_source_provider_id="inherited-session",
                accepted_source_provider_identity=identity("a"),
            )

    def test_observed_environment_checks_requirements_without_routing(self) -> None:
        observed = ObservedExecutionEnvironment(
            "linux",
            "24.04",
            "x86_64",
            64,
            262144,
            "nvidia",
            "NVIDIA RTX PRO 4500 Blackwell Generation",
            8,
            81920,
            ("cuda", "tensor-cores"),
        )
        accepted = ExecutionRequirements(
            os_family="linux",
            cpu_architecture="x86_64",
            minimum_cpu_cores=32,
            minimum_memory_mib=131072,
            gpu=GpuRequirement(
                "nvidia",
                "NVIDIA RTX PRO 4500 Blackwell Generation",
                4,
                81920,
                ("cuda",),
            ),
        )
        rejected = ExecutionRequirements(
            os_family="windows",
            minimum_cpu_cores=128,
        )

        self.assertTrue(observed.satisfies(accepted))
        self.assertFalse(observed.satisfies(rejected))
        self.assertEqual(
            ObservedExecutionEnvironment.from_dict(observed.to_dict()), observed
        )

    def test_dispatch_result_is_compact_and_binds_external_task_not_lease(self) -> None:
        observed = ObservedExecutionEnvironment(
            "linux",
            "24.04",
            "x86_64",
            32,
            65536,
            toolchain_identities=(identity("5"),),
        )
        result = ExecutionDispatchResult(
            identity("1"),
            identity("2"),
            "mac-task-123",
            DispatchResultStatus.PASSED,
            observed,
            0,
            artifact_reference("3"),
            identity("4"),
        )

        self.assertEqual(ExecutionDispatchResult.from_dict(result.to_dict()), result)
        self.assertNotIn("lease", result.to_dict())
        self.assertNotIn("hostname", result.to_dict())
        self.assertIsNone(result.to_dict()["coverage_gaps"])
        legacy = dict(result.to_dict())
        legacy.pop("coverage_gaps")
        self.assertIsNone(ExecutionDispatchResult.from_dict(legacy).coverage_gaps)

    def test_remote_evidence_contracts_round_trip_through_schema_and_result(
        self,
    ) -> None:
        observed = ObservedExecutionEnvironment(
            "linux",
            "24.04",
            "x86_64",
            32,
            65536,
            toolchain_identities=(identity("5"),),
        )
        evidence_file = RemoteEvidenceFile(
            "binary/app",
            "binary",
            6,
            identity("6"),
            True,
        )
        manifest = RemoteLifecycleEvidenceManifest(
            identity("1"),
            identity("2"),
            identity("3"),
            identity("4"),
            identity("5"),
            identity("6"),
            identity("7"),
            identity("8"),
            identity("9"),
            identity("a"),
            "build",
            "passed",
            identity("b"),
            identity("c"),
            identity("7"),
            False,
            (identity("d"),),
            (evidence_file,),
            None,
            "0" * 32,
        )
        reference = ContentReference(
            "remote-lifecycle-evidence",
            "staged:remote-evidence.tar.gz",
            identity("e"),
        )
        receipt = RemoteEvidenceCustodyReceipt(
            identity("1"),
            identity("3"),
            manifest.identity,
            reference.identity,
            identity("f"),
            (evidence_file.identity,),
            identity("7"),
            identity("0"),
        )
        result = ExecutionDispatchResult(
            identity("1"),
            identity("3"),
            "ssh-task",
            DispatchResultStatus.PASSED,
            observed,
            0,
            artifact_reference("7"),
            identity("b"),
            evidence_manifest=manifest,
            evidence_reference=reference,
            custody_receipt=receipt,
        )
        control = RemoteExecutionControlResult.from_dispatch_result(
            result,
            manifest_size=len(canonical_json_bytes(manifest.to_dict())),
            bundle_size=4096,
            redacted_summary="passed",
        )

        for contract in (evidence_file, manifest, receipt, result, control):
            with self.subTest(schema=contract.SCHEMA):
                self.schemas.validate(contract.SCHEMA, contract.to_dict())
        self.assertEqual(
            ExecutionDispatchResult.from_dict(result.to_dict()),
            result,
        )
        self.assertEqual(
            RemoteExecutionControlResult.from_dict(control.to_dict()),
            control,
        )
        self.assertEqual(
            control.bind_imported_manifest(manifest).evidence_manifest,
            manifest,
        )

    def test_every_wire_contract_is_accepted_by_the_versioned_schema(self) -> None:
        worker_catalog = workers()
        worker = worker_catalog.worker("fleet")
        requirements = worker.requirements
        gpu_requirement = GpuRequirement("nvidia", capabilities=("cuda",))
        request = ExecutionDispatchRequest(
            LifecycleDispatchAction.TEST,
            "component://example/service",
            "components/service",
            worker.target_profile,
            (),
            worker.identity,
            requirements,
            (("queue", "batch"),),
            (),
            identity("1"),
            identity("2"),
            identity("3"),
            identity("4"),
            identity("5"),
            identity("6"),
            identity("7"),
            None,
            600,
        )
        observed = ObservedExecutionEnvironment(
            "linux",
            "24.04",
            "x86_64",
            32,
            32768,
            "nvidia",
            "h100",
            1,
            81920,
            ("cuda",),
            (identity("5"),),
        )
        result = ExecutionDispatchResult(
            request.identity,
            worker.identity,
            "external-123",
            DispatchResultStatus.PASSED,
            observed,
            0,
            artifact_reference("7"),
            identity("8"),
        )

        contracts = (
            gpu_requirement,
            requirements,
            worker.parameters[0],
            worker.environment[0],
            worker,
            worker_catalog,
            request,
            observed,
            result,
        )
        for contract in contracts:
            assert contract is not None
            with self.subTest(schema=contract.SCHEMA):
                self.schemas.validate(contract.SCHEMA, contract.to_dict())
        self.schemas.validate(
            "urn:literate-ai:schema:v1:execution-worker-parameter-value",
            request.to_dict()["parameters"][0],
        )

    def test_artifact_references_are_immutable_credential_free_uris(self) -> None:
        worker = workers().worker("fleet")
        for uri in (
            "relative/path",
            "https://user:secret@example.invalid/artifact",
            "https://example.invalid/artifact?token=secret",
            "https://example.invalid/artifact#mutable",
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(ContractValidationError):
                    ExecutionDispatchRequest(
                        LifecycleDispatchAction.RUN,
                        "component://example/service",
                        "components/service",
                        worker.target_profile,
                        (),
                        worker.identity,
                        worker.requirements,
                        (("queue", "batch"),),
                        (),
                        identity("1"),
                        identity("2"),
                        identity("3"),
                        identity("4"),
                        identity("5"),
                        identity("6"),
                        identity("7"),
                        ContentReference("artifact-export", uri, identity("7")),
                        600,
                    )

        run_request = ExecutionDispatchRequest(
            LifecycleDispatchAction.RUN,
            "component://example/service",
            "components/service",
            worker.target_profile,
            (),
            worker.identity,
            worker.requirements,
            (("queue", "batch"),),
            ("hello world", "--format=json"),
            identity("1"),
            identity("2"),
            identity("3"),
            identity("4"),
            identity("5"),
            identity("6"),
            identity("7"),
            artifact_reference(),
            600,
        )
        self.assertEqual(run_request.artifact_reference, artifact_reference())


if __name__ == "__main__":
    unittest.main()
