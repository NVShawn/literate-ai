"""Read-only native package batch planning CLI."""

from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.package import (
    PACKAGE_BATCH_DECLARATION_SCHEMA,
    package_from_args,
    validate_package_worker_providers,
)
from literate_ai.contracts import (
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
    ObservedExecutionEnvironment,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.executable_components.packages import PackageKind
from tests.unit.test_package_release_contracts import (
    PackageReleaseContractTests,
    _identity,
)


def _generation_plan(*, providers: tuple[str, ...]) -> dict[str, object]:
    return {
        "schema": "literate-ai/generation-plan@6",
        "component": {
            "coordinate": "component://example/hello",
            "version": "1.0.0",
            "identity": "sha256:" + "1" * 64,
            "revision_identity": "sha256:" + "2" * 64,
        },
        "resolution": {
            "component_lock_identity": "sha256:" + "3" * 64,
            "target_profile_identity": "sha256:" + "4" * 64,
            "root_revision_identity": "sha256:" + "2" * 64,
        },
        "specifications": [{"path": "component.md", "identity": "sha256:" + "5" * 64}],
        "input_closure": {
            "identity": "sha256:" + "6" * 64,
            "file_count": 4,
            "total_bytes": 1024,
        },
        "selected_flavors": [
            {
                "id": f"package.{provider}",
                "coordinate": f"flavor://literate-ai/package-{provider}",
                "axis": "packaging",
                "value": provider,
                "revision_identity": "sha256:" + str(index + 7) * 64,
                "slot_ids": ["package"],
            }
            for index, provider in enumerate(providers)
        ],
    }


class PackageCliTests(unittest.TestCase):
    def _args(self) -> Namespace:
        return Namespace(
            package_command="plan",
            component="samples/hello-component",
            project=".",
            target="host",
            flavor=[],
            model="pipeline-model",
        )

    def test_plan_projects_multiple_exact_providers_without_authorizing_execution(self):
        generation = _generation_plan(providers=("conan", "pip"))
        with (
            mock.patch(
                "literate_ai.cli.package.discover_project",
                return_value=SimpleNamespace(root=Path("/project")),
            ),
            mock.patch(
                "literate_ai.cli.generation.plan_from_args", return_value=generation
            ) as plan,
        ):
            first, status = package_from_args(self._args())
            second, _status = package_from_args(self._args())

        self.assertEqual(status, 0)
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], PACKAGE_BATCH_DECLARATION_SCHEMA)
        self.assertEqual(
            [item["value"] for item in first["providers"]], ["conan", "pip"]
        )
        self.assertFalse(first["execution_authorized"])
        self.assertFalse(first["publication_authorized"])
        self.assertTrue(str(first["identity"]).startswith("sha256:"))
        self.assertEqual(
            plan.call_args.args[0].specification,
            str(Path("/project") / "samples" / "hello-component"),
        )
        self.assertEqual(plan.call_args.args[0].model, "pipeline-model")

    def test_plan_rejects_an_untagged_component(self):
        generation = _generation_plan(providers=())
        with (
            mock.patch(
                "literate_ai.cli.package.discover_project",
                return_value=SimpleNamespace(root=Path("/project")),
            ),
            mock.patch(
                "literate_ai.cli.generation.plan_from_args", return_value=generation
            ),
            self.assertRaises(CliFailure) as raised,
        ):
            package_from_args(self._args())

        self.assertEqual(raised.exception.code, "package.component_untagged")

    def test_build_requires_explicit_host_execution_acknowledgement(self):
        args = self._args()
        args.package_command = "build"
        args.allow_host_execution = False
        generation = _generation_plan(providers=("pip",))
        with (
            mock.patch(
                "literate_ai.cli.package.discover_project",
                return_value=SimpleNamespace(root=Path("/project")),
            ),
            mock.patch(
                "literate_ai.cli.generation.plan_from_args", return_value=generation
            ),
            self.assertRaises(CliFailure) as raised,
        ):
            package_from_args(args)

        self.assertEqual(
            raised.exception.code, "package.host_execution_not_acknowledged"
        )

    def _build_args(self) -> Namespace:
        args = self._args()
        args.package_command = "build"
        args.allow_host_execution = True
        args.jobs = 1
        args.force_regeneration = False
        args.worker = None
        args.worker_param = []
        args.worker_config = None
        args.worker_timeout_seconds = 3600
        return args

    def test_macos_worker_rejects_apt_provider(self) -> None:
        from literate_ai.cli.execution_workers import SelectedExecutionWorker

        selected = SelectedExecutionWorker(
            ExecutionWorker(
                "mac",
                ExecutionWorkerKind.SSH,
                requirements=ExecutionRequirements(os_family="macos"),
                endpoint="user@host",
                workspace="~/literate-ai",
            ),
            (),
            canonical_identity({"catalog": "test"}),
            Path("workers.json"),
        )
        with self.assertRaises(CliFailure) as raised:
            validate_package_worker_providers(selected, ["apt"])
        self.assertEqual(raised.exception.code, "package.worker_os_mismatch")

    def test_unconstrained_local_worker_allows_native_providers(self) -> None:
        from literate_ai.cli.execution_workers import SelectedExecutionWorker

        selected = SelectedExecutionWorker(
            ExecutionWorker("local", ExecutionWorkerKind.LOCAL),
            (),
            canonical_identity({"catalog": "implicit-local"}),
            None,
        )
        validate_package_worker_providers(
            selected, ["apt", "brew", "winget", "chocolatey", "pip"]
        )

    def test_build_forwards_selected_worker_and_does_not_authorize_publication(
        self,
    ) -> None:
        args = self._build_args()
        generation = _generation_plan(providers=("pip",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "samples" / "hello-component" / "component.md"
            spec.parent.mkdir(parents=True)
            spec.write_text("# hello\n", encoding="utf-8")
            args.project = str(root)
            with (
                mock.patch(
                    "literate_ai.cli.package.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                mock.patch(
                    "literate_ai.cli.generation.plan_from_args",
                    return_value=generation,
                ),
                mock.patch(
                    "literate_ai.cli.rebuild.rebuild_from_args",
                    return_value={},
                ) as rebuild,
                self.assertRaises(CliFailure) as raised,
            ):
                package_from_args(args)
        self.assertEqual(raised.exception.code, "package.execution_incomplete")
        forwarded = rebuild.call_args.args[0]
        self.assertIsNotNone(forwarded.execution_worker)
        self.assertEqual(forwarded.execution_worker.worker.worker_id, "local")
        self.assertFalse(forwarded.execution_worker.worker.kind is None)

    def test_build_dispatches_command_worker_without_artifact_export(self) -> None:
        args = self._build_args()
        args.worker = "fleet"
        generation = _generation_plan(providers=("pip",))
        worker = ExecutionWorker(
            "fleet", ExecutionWorkerKind.COMMAND, command=("dispatcher",)
        )
        identities = tuple(
            canonical_identity({"authority": index}) for index in range(7)
        )
        request = ExecutionDispatchRequest(
            LifecycleDispatchAction.BUILD,
            "component://example/hello",
            "samples/hello-component",
            "host",
            (),
            worker.identity,
            worker.requirements,
            (),
            (),
            *identities,
            None,
            60,
        )
        dispatched = ExecutionDispatchResult(
            request.identity,
            worker.identity,
            "synthetic-task",
            DispatchResultStatus.PASSED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(canonical_identity({"tool": "python"}),),
            ),
            0,
            ContentReference(
                "artifact-export",
                "cas:sha256:" + "a" * 64,
                canonical_identity({"artifact": "demo"}),
            ),
            canonical_identity({"evidence": "build"}),
            "",
            "",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "samples" / "hello-component" / "component.md"
            spec.parent.mkdir(parents=True)
            spec.write_text("# hello\n", encoding="utf-8")
            args.project = str(root)
            catalog = root / "workers.json"
            catalog.write_bytes(
                canonical_json_bytes(ExecutionWorkerCatalog((worker,)).to_dict())
            )
            args.worker_config = str(catalog)
            with (
                mock.patch(
                    "literate_ai.cli.package.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                mock.patch(
                    "literate_ai.cli.generation.plan_from_args",
                    return_value=generation,
                ),
                mock.patch(
                    "literate_ai.cli.execution_workers.create_execution_dispatch_request",
                    return_value=request,
                ),
                mock.patch(
                    "literate_ai.cli.package.CommandExecutionDispatcher"
                ) as dispatcher_cls,
                mock.patch(
                    "literate_ai.adapters.artifact_exports.record_remote_artifact_export",
                ) as export,
                mock.patch(
                    "literate_ai.cli.rebuild.rebuild_from_args",
                    return_value={},
                ),
                self.assertRaises(CliFailure) as raised,
            ):
                dispatcher_cls.return_value.dispatch.return_value = dispatched
                package_from_args(args)
        self.assertEqual(raised.exception.code, "package.execution_incomplete")
        dispatcher_cls.return_value.dispatch.assert_called_once()
        export.assert_not_called()
        dispatched_request = dispatcher_cls.return_value.dispatch.call_args.args[1]
        self.assertEqual(dispatched_request.action, LifecycleDispatchAction.BUILD)

    def test_npm_public_plan_build_verify_and_stale_input_refusal(self) -> None:
        self._public_archive_lifecycle("npm")

    def test_zip_public_plan_build_verify_and_stale_input_refusal(self) -> None:
        self._public_archive_lifecycle("zip")

    def _public_archive_lifecycle(self, provider: str) -> None:
        generation = _generation_plan(providers=(provider,))
        release = PackageReleaseContractTests()
        release.setUp()
        base_plan = release.plan(PackageKind.DIRECTORY, ())
        contents = {
            item.blob.identity: item.export_id.encode()
            for manifest in release.graph.manifests
            for item in manifest.exports
        }
        base_result = DirectoryPackageAdapter().package(
            base_plan, read_blob=lambda reference: contents[reference.identity]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specification = root / "components" / "javascript" / "component.md"
            specification.parent.mkdir(parents=True)
            specification.write_text("# Synthetic JavaScript\n", encoding="utf-8")
            custody_root = root / "accepted-package"
            for item in base_plan.inputs:
                path = custody_root.joinpath(*Path(item.path).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents[item.blob.identity])
            object_root = root / "obj"
            object_root.mkdir()
            source_sbom = b'{"bomFormat":"CycloneDX","lifecycle":"source"}'
            resolved_sbom = b'{"bomFormat":"CycloneDX","lifecycle":"resolved"}'
            build_evidence = SimpleNamespace(
                identity=_identity("npm-build-evidence"),
                source_sbom=SimpleNamespace(bom_identity=_identity("npm-source-sbom")),
            )
            lifecycle = SimpleNamespace(
                identity=_identity("npm-lifecycle"),
                root_integration=SimpleNamespace(
                    package_plan=base_plan,
                    package_result=base_result,
                ),
                node_results=(
                    SimpleNamespace(
                        component_revision=base_plan.root_component_revision,
                        build_evidence=build_evidence,
                    ),
                ),
            )
            rebuilt = SimpleNamespace(execution=SimpleNamespace(lifecycle=lifecycle))
            ports = SimpleNamespace(
                project_package_custody=lambda *_args: SimpleNamespace(
                    root=custody_root
                ),
                source_sbom_content=lambda _evidence: source_sbom,
                resolved_sbom_content=lambda _evidence: resolved_sbom,
            )
            adapter = SimpleNamespace(runtime=SimpleNamespace(lifecycle_ports=ports))
            directories = SimpleNamespace(obj_dir=object_root)

            def rebuild(_args, *, standard_observer):
                standard_observer(rebuilt, adapter, None, directories)

            plan_args = self._args()
            plan_args.project = str(root)
            plan_args.component = "components/javascript"
            project = SimpleNamespace(root=root)
            with (
                mock.patch(
                    "literate_ai.cli.package.discover_project", return_value=project
                ),
                mock.patch(
                    "literate_ai.cli.generation.plan_from_args",
                    return_value=generation,
                ),
            ):
                declaration, status = package_from_args(plan_args)
            self.assertEqual(status, 0)
            self.assertEqual(declaration["providers"][0]["value"], provider)
            self.assertFalse(declaration["execution_authorized"])

            args = self._build_args()
            args.project = str(root)
            args.component = "components/javascript"
            with (
                mock.patch(
                    "literate_ai.cli.package.discover_project", return_value=project
                ),
                mock.patch(
                    "literate_ai.cli.generation.plan_from_args",
                    return_value=generation,
                ),
                mock.patch(
                    "literate_ai.cli.rebuild.rebuild_from_args", side_effect=rebuild
                ),
            ):
                built, status = package_from_args(args)
            self.assertEqual(status, 0)
            self.assertEqual(built["packages"][0]["provider"], provider)
            self.assertTrue(Path(built["packages"][0]["artifact"]).is_file())
            self.assertFalse(built["publication_authorized"])

            args.package_command = "verify"
            with (
                mock.patch(
                    "literate_ai.cli.package.discover_project", return_value=project
                ),
                mock.patch(
                    "literate_ai.cli.generation.plan_from_args",
                    return_value=generation,
                ),
                mock.patch(
                    "literate_ai.cli.package.resolve_cache_directories",
                    return_value=directories,
                ),
            ):
                verified, status = package_from_args(args)
            self.assertEqual(status, 0)
            self.assertEqual(verified["verified"][0]["provider"], provider)
            self.assertFalse(verified["publication_authorized"])

            stale = {**generation, "input_closure": {"identity": "sha256:" + "f" * 64}}
            with (
                mock.patch(
                    "literate_ai.cli.package.discover_project", return_value=project
                ),
                mock.patch(
                    "literate_ai.cli.generation.plan_from_args", return_value=stale
                ),
                mock.patch(
                    "literate_ai.cli.package.resolve_cache_directories",
                    return_value=directories,
                ),
                self.assertRaises(CliFailure) as raised,
            ):
                package_from_args(args)
            self.assertEqual(raised.exception.code, "package.declaration_changed")

    def test_apt_public_build_and_verify_selects_real_debian_adapter(self) -> None:
        from tests.unit.test_debian_packaging import DebianPackagingTests

        generation = _generation_plan(providers=("apt",))
        release = PackageReleaseContractTests()
        release.setUp()
        base_plan = release.plan(PackageKind.DIRECTORY, ())
        contents = {
            item.blob.identity: item.export_id.encode()
            for manifest in release.graph.manifests
            for item in manifest.exports
        }
        base_result = DirectoryPackageAdapter().package(
            base_plan, read_blob=lambda reference: contents[reference.identity]
        )
        debian = DebianPackagingTests()
        debian.setUp()
        self.addCleanup(debian.tearDown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specification = root / "components" / "native" / "component.md"
            specification.parent.mkdir(parents=True)
            specification.write_text(
                "# Synthetic native application\n", encoding="utf-8"
            )
            custody_root = root / "accepted-package"
            for item in base_plan.inputs:
                path = custody_root.joinpath(*Path(item.path).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents[item.blob.identity])
            object_root = root / "obj"
            object_root.mkdir()
            source_sbom = b'{"bomFormat":"CycloneDX","components":[]}'
            resolved_sbom = b'{"bomFormat":"CycloneDX","components":[]}'
            build_evidence = SimpleNamespace(
                identity=_identity("apt-build-evidence"),
                source_sbom=SimpleNamespace(bom_identity=_identity("apt-source-sbom")),
            )
            lifecycle = SimpleNamespace(
                identity=_identity("apt-lifecycle"),
                root_integration=SimpleNamespace(
                    package_plan=base_plan,
                    package_result=base_result,
                ),
                node_results=(
                    SimpleNamespace(
                        component_revision=base_plan.root_component_revision,
                        build_evidence=build_evidence,
                    ),
                ),
            )
            rebuilt = SimpleNamespace(execution=SimpleNamespace(lifecycle=lifecycle))
            ports = SimpleNamespace(
                project_package_custody=lambda *_args: SimpleNamespace(
                    root=custody_root
                ),
                source_sbom_content=lambda _evidence: source_sbom,
                resolved_sbom_content=lambda _evidence: resolved_sbom,
            )
            adapter = SimpleNamespace(runtime=SimpleNamespace(lifecycle_ports=ports))
            directories = SimpleNamespace(obj_dir=object_root)

            def rebuild(_args, *, standard_observer):
                standard_observer(rebuilt, adapter, None, directories)

            args = self._build_args()
            args.project = str(root)
            args.component = "components/native"
            project = SimpleNamespace(root=root)
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.package.discover_project", return_value=project
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.generation.plan_from_args",
                        return_value=generation,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.package.DpkgDebToolBinding.discover",
                        return_value=debian.tool,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.package.project_debian_package",
                        side_effect=debian._capture_projection,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.rebuild.rebuild_from_args",
                        side_effect=rebuild,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.adapters.debian_packaging.platform.system",
                        return_value="Linux",
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.adapters.debian_packaging.run_with_tree_kill",
                        side_effect=debian._fake_dpkg,
                    )
                )
                built, status = package_from_args(args)
            self.assertEqual(status, 0)
            artifact = Path(built["packages"][0]["artifact"])
            self.assertEqual(built["packages"][0]["provider"], "apt")
            self.assertEqual(artifact.suffix, ".deb")
            self.assertTrue(artifact.read_bytes().startswith(b"!<arch>\n"))

            args.package_command = "verify"
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.package.discover_project", return_value=project
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.generation.plan_from_args",
                        return_value=generation,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.package.DpkgDebToolBinding.discover",
                        return_value=debian.tool,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "literate_ai.cli.package.resolve_cache_directories",
                        return_value=directories,
                    )
                )
                verified, status = package_from_args(args)
            self.assertEqual(status, 0)
            self.assertEqual(verified["verified"][0]["provider"], "apt")
            self.assertFalse(verified["publication_authorized"])


if __name__ == "__main__":
    unittest.main()
