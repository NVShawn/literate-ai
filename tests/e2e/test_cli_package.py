"""Read-only native package batch planning CLI."""

from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.package import (
    package_from_args,
)
from literate_ai.contracts.executable_components.packages import PackageKind
from tests.support import fixtures_test_package_release_contracts as release_fixtures
from tests.support.fixtures_test_package_release_contracts import _identity


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

    def test_npm_public_plan_build_verify_and_stale_input_refusal(self) -> None:
        self._public_archive_lifecycle("npm")

    def test_zip_public_plan_build_verify_and_stale_input_refusal(self) -> None:
        self._public_archive_lifecycle("zip")

    def _public_archive_lifecycle(self, provider: str) -> None:
        generation = _generation_plan(providers=(provider,))
        release = release_fixtures.PackageReleaseContractTests()
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


if __name__ == "__main__":
    unittest.main()
