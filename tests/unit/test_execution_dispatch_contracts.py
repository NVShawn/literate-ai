from __future__ import annotations

import unittest

from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    ExecutionDispatchRequest,
    ExecutionRequirements,
    ExecutionSourceMaterialization,
    ExecutionSourceMaterializationKind,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
    ExecutionWorkerParameter,
    HashAlgorithm,
    LifecycleDispatchAction,
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
