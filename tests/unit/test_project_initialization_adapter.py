"""Focused tests for the public filesystem project-initialization adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter as DefaultParentProjectInitializationAdapter,
)
from literate_ai.adapters.project_initialization import (
    ProjectInitializationError,
    _atomic_replace,
    _default_repository_parent,
    _initialization_prerequisites,
    apply_init_flavor_defaults,
    discover_installed_initialization_origin,
    host_platform_selector,
    plan_convert,
    record_project_authority_review,
)
from literate_ai.adapters.project_initialization import (
    initialize_project as _initialize_project,
)
from literate_ai.adapters.project_validation import validate_project
from literate_ai.adapters.repository_catalogs import (
    InheritedCatalogFile,
    InheritedCatalogItem,
    InheritedCatalogPlan,
    plan_inherited_catalogs,
)
from literate_ai.adapters.repository_lineage import GitRepositorySnapshotProvider
from literate_ai.application.project_guidance import project_guidance
from literate_ai.application.repository_lineage import (
    RepositoryCatalogFile,
    RepositoryLineageResolutionError,
    ResolvedRepositoryCatalog,
)
from literate_ai.contracts import (
    CatalogImportsFile,
    ProjectDefinition,
    ProjectInitializationBaseline,
    ProjectInitializationOrigin,
    ProjectTestReceiptPolicy,
    RepositoryFetchDeadlinePolicy,
    RepositoryParentReference,
    RepositoryParentSelection,
    StandardLanguageCommandProfile,
    StandardProjectLifecycleDriver,
    canonical_identity,
    load_current_standard_lifecycle_policy,
)
from literate_ai.version import DISTRIBUTION_VERSION, EXPECTED_RELEASE_TAG
from tests.unit.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)
from tests.unit.test_repository_lineage import fixture

DEFAULT_TEST_FLAVORS = ("+bazel", "+python", "+macos")


def initialize_project(*args, **kwargs):
    kwargs.setdefault("parent_selection", RepositoryParentSelection.root())
    return _initialize_project(*args, **kwargs)


class FilesystemProjectInitializationAdapterTests(unittest.TestCase):
    def test_prerequisite_report_is_path_based_and_does_not_invoke_coding_cli(
        self,
    ) -> None:
        with (
            mock.patch.dict(os.environ, {"PATH": "/tools", "CODING_CLI": "codex"}),
            mock.patch(
                "literate_ai.adapters.project_initialization.shutil.which",
                side_effect=lambda name, **_kwargs: (
                    "/tools/codex" if name == "codex" else None
                ),
            ) as which,
        ):
            report = _initialization_prerequisites()

        self.assertEqual(
            report["schema"], "literate-ai/project-initialization-prerequisites@1"
        )
        self.assertEqual(report["python"]["state"], "available-runtime")
        self.assertEqual(
            set(report),
            {"schema", "python", "coding_cli"},
        )
        self.assertEqual(
            report["coding_cli"],
            {
                "required": False,
                "required_for": "source-generation",
                "name": "codex",
                "command": "/tools/codex",
                "state": "available-path",
            },
        )
        which.assert_called_once_with("codex", path="/tools")

    def test_prerequisite_report_allows_init_without_a_coding_cli(self) -> None:
        with (
            mock.patch.dict(os.environ, {"PATH": "/empty"}, clear=False),
            mock.patch(
                "literate_ai.adapters.project_initialization.shutil.which",
                return_value=None,
            ),
        ):
            report = _initialization_prerequisites()

        self.assertEqual(report["coding_cli"]["state"], "unavailable-path")
        self.assertFalse(report["coding_cli"]["required"])

    def test_initialization_observes_prerequisites_before_scaffolding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "preflight-first"

            def observe():
                self.assertFalse(target.exists())
                raise RuntimeError("preflight sentinel")

            with mock.patch(
                "literate_ai.adapters.project_initialization."
                "_initialization_prerequisites",
                side_effect=observe,
            ):
                with self.assertRaisesRegex(RuntimeError, "preflight sentinel"):
                    FilesystemProjectInitializationAdapter(
                        standard_binding_provider=lambda: None,
                        initialization_origin_provider=self._origin,
                    ).initialize(
                        target,
                        flavor_selectors=DEFAULT_TEST_FLAVORS,
                        source_intelligence_provider="none",
                    )

            self.assertFalse(target.exists())

    @staticmethod
    def _origin() -> ProjectInitializationOrigin:
        return ProjectInitializationOrigin(
            repository_url="ssh://git.example.test/operator/literate-ai.git",
            git_revision="a" * 40,
            distribution_name="literate-ai",
            distribution_version="0.2.0",
        )

    def test_initialization_records_exact_origin_and_framework_file_baseline(
        self,
    ) -> None:
        origin = self._origin()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "baseline"
            result = FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: origin,
            ).initialize(
                target,
                flavor_selectors=DEFAULT_TEST_FLAVORS,
                source_intelligence_provider="none",
            )
            origin_document = json.loads(
                (target / ".literate/initialization-origin.json").read_text(
                    encoding="utf-8"
                )
            )
            baseline_document = json.loads(
                (target / ".literate/initialization-baseline.json").read_text(
                    encoding="utf-8"
                )
            )
            recorded_origin = ProjectInitializationOrigin.from_dict(origin_document)
            baseline = ProjectInitializationBaseline.from_dict(baseline_document)
            imports = CatalogImportsFile.load(target)

            self.assertEqual(recorded_origin, origin)
            self.assertEqual(baseline.origin_identity, origin.identity)
            self.assertEqual(imports.imports, ())
            self.assertIsNone(result["inherited_catalogs"]["provenance"])
            self.assertEqual(
                result["initialization_baseline"]["identity"], baseline.identity.uri
            )
            files = {item.path: item for item in baseline.files}
            self.assertIn(".literate/initialization-origin.json", files)
            self.assertNotIn(".literate/initialization-baseline.json", files)
            self.assertEqual(
                set(files),
                set(result["created"]) - {".literate/initialization-baseline.json"},
            )
            for relative, record in files.items():
                content = target.joinpath(*Path(relative).parts).read_bytes()
                self.assertEqual(record.size, len(content))
                self.assertEqual(
                    record.identity.uri, "sha256:" + hashlib.sha256(content).hexdigest()
                )

    def test_initial_locks_observe_persisted_repository_lineage(self) -> None:
        from literate_ai.project_authority_graph import project_authority_graph

        observed_lineage: list[bool] = []

        def observe(root: Path, *, include_component_locks: bool = True):
            observed_lineage.append(
                (root / ".literate/repository-parent.json").is_file()
                and (root / ".literate/repository-lineage.json").is_file()
            )
            return project_authority_graph(
                root, include_component_locks=include_component_locks
            )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "lineage-bound-locks"
            with mock.patch(
                "literate_ai.project_authority_graph.project_authority_graph",
                side_effect=observe,
            ):
                FilesystemProjectInitializationAdapter(
                    standard_binding_provider=lambda: None,
                    initialization_origin_provider=self._origin,
                ).initialize(
                    target,
                    flavor_selectors=DEFAULT_TEST_FLAVORS,
                    source_intelligence_provider="none",
                )

        self.assertTrue(observed_lineage)
        self.assertTrue(all(observed_lineage))

    def test_explicit_parent_materializes_catalogs_before_validation(self) -> None:
        selection, _root_node, child_node, lineage = fixture()
        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "literate_ai"
            / "project_template"
            / "flavors"
            / "lang-python"
        )
        inherited = InheritedCatalogItem(
            "flavor",
            "python",
            child_node,
            tuple(
                InheritedCatalogFile(
                    path.relative_to(source.parent.parent).as_posix(),
                    path.read_bytes(),
                    False,
                )
                for path in sorted(source.rglob("*"))
                if path.is_file()
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "inherited"
            result = FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=self._origin,
                repository_lineage_resolver=lambda _selection: lineage,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    lineage, (inherited,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )

            self.assertEqual(result["inherited_catalogs"]["item_count"], 1)
            self.assertEqual(result["inherited_catalogs"]["source_count"], 1)
            self.assertEqual(
                result["inherited_catalogs"]["provenance"],
                ".literate/imports.json",
            )
            self.assertIn(".literate/imports.json", result["created"])
            provenance = json.loads(
                (target / ".literate" / "imports.json").read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["imports"][0]["name"], "python")
            self.assertEqual(
                RepositoryParentSelection.from_dict(
                    json.loads(
                        (target / ".literate/repository-parent.json").read_text(
                            encoding="utf-8"
                        )
                    )
                ),
                selection,
            )

    def test_inherited_component_retains_global_workflow_and_routing_authority(
        self,
    ) -> None:
        selection, root_node, child_node, lineage = fixture()
        repository_root = Path(__file__).resolve().parents[2]

        def project_definition(project_id: str) -> ProjectDefinition:
            value = json.loads((repository_root / "literate.project.json").read_bytes())
            value["project_id"] = project_id
            return ProjectDefinition.from_dict(value)

        inherited_paths = (
            *sorted((repository_root / "components/document-pair").rglob("*")),
            *sorted(
                (
                    repository_root / "skills/specification-to-source/document-pair"
                ).rglob("*")
            ),
            repository_root / "workflows/sample-host.md",
            repository_root / "routing/sample-host.json",
        )
        catalogs = {
            root_node.project_id: ResolvedRepositoryCatalog(
                root_node, project_definition(root_node.project_id), ()
            ),
            child_node.project_id: ResolvedRepositoryCatalog(
                child_node,
                project_definition(child_node.project_id),
                tuple(
                    RepositoryCatalogFile(
                        path.relative_to(repository_root).as_posix(), path.read_bytes()
                    )
                    for path in sorted(
                        path for path in inherited_paths if path.is_file()
                    )
                ),
            ),
        }

        class Provider:
            @staticmethod
            def catalog(node):
                return catalogs[node.project_id]

        inherited = plan_inherited_catalogs(lineage, Provider())
        self.assertIn("workflow", {item.kind for item in inherited.items})
        self.assertIn("routing", {item.kind for item in inherited.items})

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "inherited-component"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=self._origin,
                repository_lineage_resolver=lambda _selection: lineage,
                repository_catalog_planner=lambda _lineage: inherited,
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )

            self.assertTrue(
                (target / "components/document-pair/component.md").is_file()
            )
            self.assertTrue((target / "workflows/sample-host.md").is_file())
            self.assertTrue((target / "routing/sample-host.json").is_file())
            validate_project(
                target,
                require_authority_review=False,
                synchronize_source_intelligence=False,
            )

    def test_upstream_flavor_catalogs_can_lock_the_starter_component(self) -> None:
        selection, _root_node, child_node, lineage = fixture()
        repository = Path(__file__).resolve().parents[2]
        inherited = []
        for name in ("lang-python", "os-macos"):
            source = repository / "flavors" / name
            inherited.append(
                InheritedCatalogItem(
                    "flavor",
                    name,
                    child_node,
                    tuple(
                        InheritedCatalogFile(
                            path.relative_to(repository).as_posix(),
                            path.read_bytes(),
                            False,
                        )
                        for path in sorted(source.rglob("*"))
                        if path.is_file()
                    ),
                )
            )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "inherited-starter"
            result = FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=self._origin,
                repository_lineage_resolver=lambda _selection: lineage,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    lineage, tuple(inherited)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                parent_selection=selection,
            )

            self.assertIsNotNone(result["initial_lock"])
            lock = json.loads(
                (target / "samples/hello-component/component.lock.json").read_text(
                    encoding="utf-8"
                )
            )
            selected_values = {
                selected["value"]
                for slot in lock["nodes"][0]["target_flavor_selection"]["slots"]
                for selected in slot["selected"]
            }
            self.assertEqual(selected_values, {"python", "macos"})

    def test_initialization_preserves_git_metadata_and_existing_readme(self) -> None:
        origin = self._origin()
        readme = b"# Existing repository\n\nOperator-owned introduction.\n"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "repository"
            target.mkdir()
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet"),
                check=True,
                capture_output=True,
            )
            git_sentinel = target / ".git" / "litai-preserved"
            git_sentinel.write_bytes(b"repository metadata\n")
            (target / "README.md").write_bytes(readme)

            result = FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: origin,
            ).initialize(
                target,
                flavor_selectors=DEFAULT_TEST_FLAVORS,
                source_intelligence_provider="none",
            )
            baseline = ProjectInitializationBaseline.from_dict(
                json.loads(
                    (target / ".literate/initialization-baseline.json").read_text(
                        encoding="utf-8"
                    )
                )
            )

            self.assertEqual((target / "README.md").read_bytes(), readme)
            self.assertEqual(git_sentinel.read_bytes(), b"repository metadata\n")
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertNotIn("README.md", result["created"])
            self.assertNotIn("README.md", {item.path for item in baseline.files})

    def test_initialization_rejects_existing_source_until_adoption_exists(self) -> None:
        origin = self._origin()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source-repository"
            target.mkdir()
            (target / ".git").mkdir()
            source = target / "application.py"
            source.write_bytes(b"print('existing source')\n")

            with self.assertRaises(ProjectInitializationError) as raised:
                FilesystemProjectInitializationAdapter(
                    standard_binding_provider=lambda: None,
                    initialization_origin_provider=lambda: origin,
                ).initialize(
                    target,
                    flavor_selectors=DEFAULT_TEST_FLAVORS,
                    source_intelligence_provider="none",
                )

            self.assertEqual(raised.exception.code, "project.init_target_not_empty")
            self.assertIn("init --convert", raised.exception.message)
            self.assertEqual(source.read_bytes(), b"print('existing source')\n")
            self.assertFalse((target / "literate.project.json").exists())

    def test_initialization_rejects_unsafe_preserved_bootstrap_entries(self) -> None:
        origin = self._origin()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "unsafe-repository"
            target.mkdir()
            (target / ".git").write_bytes(b"gitdir: elsewhere\n")

            with self.assertRaises(ProjectInitializationError) as raised:
                FilesystemProjectInitializationAdapter(
                    standard_binding_provider=lambda: None,
                    initialization_origin_provider=lambda: origin,
                ).initialize(
                    target,
                    flavor_selectors=DEFAULT_TEST_FLAVORS,
                    source_intelligence_provider="none",
                )

            self.assertEqual(raised.exception.code, "project.init_target_invalid")
            self.assertFalse((target / "literate.project.json").exists())

    def test_git_origin_discovery_preserves_an_operator_fork(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()
            commands = (
                ("init",),
                ("config", "user.email", "test@example.test"),
                ("config", "user.name", "Test"),
                ("remote", "add", "origin", "ssh://git.example.test/operator/fork.git"),
            )
            for arguments in commands:
                subprocess.run(
                    ("git", "-C", str(checkout), *arguments),
                    check=True,
                    capture_output=True,
                )
            (checkout / "README.md").write_text("fork\n", encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(checkout), "add", "README.md"),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "-C", str(checkout), "commit", "-m", "fixture"),
                check=True,
                capture_output=True,
            )

            origin = discover_installed_initialization_origin(
                source_roots=(checkout,), direct_url={}
            )

        self.assertEqual(
            origin.repository_url, "ssh://git.example.test/operator/fork.git"
        )
        self.assertRegex(origin.git_revision, r"^[0-9a-f]{40}$")

    def test_non_git_origin_fails_before_project_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "project"

            def unavailable() -> ProjectInitializationOrigin:
                return discover_installed_initialization_origin(
                    source_roots=(root,), direct_url={}
                )

            with self.assertRaises(ProjectInitializationError) as raised:
                FilesystemProjectInitializationAdapter(
                    standard_binding_provider=lambda: None,
                    initialization_origin_provider=unavailable,
                ).initialize(
                    target,
                    flavor_selectors=DEFAULT_TEST_FLAVORS,
                    source_intelligence_provider="none",
                )

            self.assertEqual(raised.exception.code, "project.init_origin_unavailable")
            self.assertFalse(target.exists())

    def test_repository_cycle_fails_before_project_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            selection = RepositoryParentSelection.inherit(
                (RepositoryParentReference("https://example.test/cyclic.git", "main"),)
            )

            def cyclic(_selection):
                raise RepositoryLineageResolutionError(
                    "repository_lineage.cycle",
                    "repository parent declarations contain a cycle: "
                    "repository:a -> repository:b -> repository:a",
                )

            with self.assertRaises(ProjectInitializationError) as raised:
                FilesystemProjectInitializationAdapter(
                    standard_binding_provider=lambda: None,
                    initialization_origin_provider=self._origin,
                    repository_lineage_resolver=cyclic,
                ).initialize(
                    target,
                    flavor_selectors=DEFAULT_TEST_FLAVORS,
                    source_intelligence_provider="none",
                    parent_selection=selection,
                )

            self.assertEqual(raised.exception.code, "repository_lineage.cycle")
            self.assertFalse(target.exists())

    def test_installed_distribution_origin_survives_without_a_checkout(self) -> None:
        origin = self._origin()
        with (
            mock.patch(
                "literate_ai.adapters.project_initialization._git_origin",
                return_value=None,
            ),
            mock.patch(
                "literate_ai.adapters.project_initialization."
                "_installed_distribution_origin",
                return_value=origin,
            ),
        ):
            discovered = discover_installed_initialization_origin(
                source_roots=(Path("outside-checkout"),), direct_url={}
            )

        self.assertEqual(discovered, origin)

    def test_installed_origin_default_parent_tracks_highest_release_not_head(
        self,
    ) -> None:
        origin = self._origin()
        tags = (
            "HEAD",
            "v0.6.4",
            "v0.7.2",
            EXPECTED_RELEASE_TAG,
            "v9.9.9",
            "v0.8.0-rc.1",
        )
        with mock.patch(
            "literate_ai.adapters.project_initialization._source_checkout",
            return_value=False,
        ):
            selection = _default_repository_parent(
                origin, release_tags=lambda _url: tags
            )

        self.assertEqual(len(selection.parents), 1)
        self.assertEqual(selection.parents[0].repository_url, origin.repository_url)
        self.assertEqual(selection.parents[0].requested_revision, EXPECTED_RELEASE_TAG)
        self.assertNotEqual(selection.parents[0].requested_revision, "HEAD")
        self.assertNotEqual(
            selection.parents[0].requested_revision, origin.git_revision
        )

    def test_default_parent_fails_closed_when_no_published_release_tag_exists(
        self,
    ) -> None:
        origin = self._origin()
        with mock.patch(
            "literate_ai.adapters.project_initialization._source_checkout",
            return_value=False,
        ):
            with self.assertRaises(ProjectInitializationError) as raised:
                _default_repository_parent(
                    origin, release_tags=lambda _url: ("HEAD", "v0.8.0-rc.1")
                )

        self.assertEqual(
            raised.exception.code, "repository_lineage.release_unavailable"
        )
        self.assertIn(DISTRIBUTION_VERSION, raised.exception.message)

    def test_embedded_origin_outranks_install_transport_checkout(self) -> None:
        embedded = self._origin()
        transport = ProjectInitializationOrigin(
            repository_url="file:///temporary/clean-projection",
            git_revision=embedded.git_revision,
            distribution_name="literate-ai",
            distribution_version="0.2.0",
        )
        with (
            mock.patch(
                "literate_ai.adapters.project_initialization."
                "_installed_distribution_origin",
                return_value=embedded,
            ),
            mock.patch(
                "literate_ai.adapters.project_initialization._git_origin",
                return_value=transport,
            ) as git_origin,
        ):
            discovered = discover_installed_initialization_origin(
                source_roots=(Path("transport-checkout"),),
                direct_url={
                    "url": "file:///temporary/clean-projection",
                    "dir_info": {},
                },
            )

        self.assertEqual(discovered, embedded)
        git_origin.assert_not_called()

    def test_multiple_distribution_origins_fail_closed_as_ambiguous(self) -> None:
        first = self._origin()
        second = ProjectInitializationOrigin(
            repository_url="ssh://git.example.test/other/literate-ai.git",
            git_revision="b" * 40,
            distribution_name="literate-ai",
            distribution_version="0.2.0",
        )
        with mock.patch(
            "literate_ai.adapters.project_initialization._git_origin",
            side_effect=(first, second),
        ):
            with self.assertRaises(ProjectInitializationError) as raised:
                discover_installed_initialization_origin(
                    source_roots=(Path("first"), Path("second")), direct_url={}
                )

        self.assertEqual(raised.exception.code, "project.init_origin_ambiguous")

    def test_credential_bearing_repository_origin_is_never_persistable(self) -> None:
        for repository_url in (
            "https://token@example.test/operator/literate-ai.git",
            "https://example.test/operator/literate-ai.git?access_token=secret",
            "ssh://user:password@example.test/operator/literate-ai.git",
        ):
            with self.subTest(repository_url=repository_url):
                with self.assertRaisesRegex(ValueError, "must not contain credentials"):
                    ProjectInitializationOrigin(
                        repository_url,
                        "a" * 40,
                        "literate-ai",
                        "0.2.0",
                    )

    def test_atomic_replace_uses_path_chmod_when_fchmod_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "authority.json"
            target.write_bytes(b"old")
            target.chmod(0o640)
            with (
                mock.patch(
                    "literate_ai.adapters.project_initialization.os.fchmod",
                    None,
                    create=True,
                ),
                mock.patch(
                    "literate_ai.adapters.project_initialization.os.chmod",
                    wraps=os.chmod,
                ) as chmod,
            ):
                _atomic_replace(target, b"new")

            chmod.assert_called_once()
            self.assertEqual(target.read_bytes(), b"new")
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_initialization_persists_an_exact_injected_standard_binding(self) -> None:
        policy = load_current_standard_lifecycle_policy()
        driver = StandardProjectLifecycleDriver(
            canonical_identity({"fixture": "installed-wheel"}),
            policy.identity,
        )
        receipt_policy = ProjectTestReceiptPolicy(
            policy.policy_id,
            policy.policy_version,
            driver.identity,
            policy.required_evidence_kinds,
            policy.minimum_test_count,
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "standard-bound"
            result = FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: (driver, receipt_policy)
            ).initialize(
                target,
                flavor_selectors=DEFAULT_TEST_FLAVORS,
                source_intelligence_provider="none",
            )
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["standard_binding"], "configured")
        self.assertEqual(manifest["lifecycle_driver"], driver.to_dict())
        self.assertEqual(manifest["test_receipt_policy"], receipt_policy.to_dict())

    def test_initialization_exposes_complete_reviewed_project_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "public-adapter"

            result = initialize_project(
                target,
                flavor_selectors=DEFAULT_TEST_FLAVORS,
                project_id="public-adapter",
                source_intelligence_provider="none",
            )
            validation = validate_project(
                target,
                require_authority_review=True,
                include_test_receipt=False,
                synchronize_source_intelligence=False,
            )

        self.assertEqual(result["schema"], "literate-ai/project-initialization@6")
        self.assertEqual(result["project_id"], "public-adapter")
        self.assertEqual(
            result["tool_bootstrap"],
            {
                "state": "disabled",
                "reason": "no-source-intelligence-provider",
            },
        )
        self.assertNotEqual(
            result["project_identity"], result["project_authority_identity"]
        )
        self.assertEqual(
            result["project_authority_identity"],
            validation["authority_review"]["authority_identity"],
        )
        self.assertEqual(validation["authority_review"]["state"], "current")
        self.assertEqual(
            result["source_intelligence"]["status"]["state"],
            "off",
        )

    def test_explicit_review_recording_refreshes_layered_catalog_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "layered"
            initialized = initialize_project(
                target,
                flavor_selectors=DEFAULT_TEST_FLAVORS,
                source_intelligence_provider="none",
            )
            routing = (
                target / "routing" / "production" / "staging" / "dev" / "routing.json"
            )
            routing.write_text(
                routing.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
                newline="\n",
            )

            current = record_project_authority_review(target)
            validation = validate_project(
                target,
                require_authority_review=True,
                include_test_receipt=False,
                synchronize_source_intelligence=False,
            )

        self.assertEqual(current["state"], "current")
        self.assertNotEqual(
            initialized["project_authority_identity"],
            current["authority_identity"],
        )
        self.assertEqual(
            current["authority_identity"],
            validation["authority_review"]["authority_identity"],
        )

    def test_missing_marker_records_a_unique_placeholder_or_requires_a_document(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing-marker"
            initialize_project(
                target,
                flavor_selectors=DEFAULT_TEST_FLAVORS,
                source_intelligence_provider="none",
            )
            traceability = target / "docs" / "architecture" / "design-traceability.md"
            content = traceability.read_text(encoding="utf-8")
            marker_start = content.index("<!-- literate-ai:authority-reviewed ")
            marker_end = content.index("-->", marker_start) + len("-->")
            pending = (
                content[:marker_start]
                + "<!-- literate-ai:authority-review-pending -->"
                + content[marker_end:]
            )
            traceability.write_text(pending, encoding="utf-8", newline="\n")
            adapter = FilesystemProjectInitializationAdapter()

            current = adapter.record_authority_review(target)
            self.assertEqual(current["state"], "current")
            self.assertTrue(current["recorded"])
            self.assertIn(
                current["expected_marker"],
                traceability.read_text(encoding="utf-8"),
            )

            traceability.write_text("# No marker\n", encoding="utf-8", newline="\n")
            with self.assertRaises(ProjectInitializationError) as raised:
                adapter.record_authority_review(target)
            self.assertEqual(
                raised.exception.code,
                "project.documentation_authority_review_document_required",
            )

    def test_cli_delegates_and_exposes_complete_initialization_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = GitRepositorySnapshotProvider(
                Path(directory) / "repository-lineage",
                deadline_policy=RepositoryFetchDeadlinePolicy(),
                deadline_provenance="framework-default",
            )
            deadline = provider.deadline_evidence

        self.assertEqual(deadline["provenance"], "framework-default")
        self.assertEqual(
            deadline["policy"],
            {
                "schema": "urn:literate-ai:schema:v1:repository-fetch-deadline-policy",
                "total_seconds": 3600,
                "no_progress_seconds": 600,
                "connect_seconds": 30,
            },
        )

    def test_public_adapter_preserves_manifest_and_template_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "template"
            result = initialize_project(
                target,
                source_intelligence_provider="none",
                flavor_selectors=("+bazel", "+python", "+macos"),
            )
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )

            self.assertTrue((target / "CHANGELOG.md").is_file())
            self.assertTrue((target / "docs/roadmap/active-work.md").is_file())
            self.assertTrue((target / "docs/user/test-matrix.md").is_file())
            self.assertTrue((target / "literate.test.example.json").is_file())
            self.assertTrue((target / "literate.workers.example.json").is_file())
            self.assertTrue((target / "literate.release.json").is_file())
            self.assertTrue(
                (target / "skills/agent/record-user-directed-work/SKILL.md").is_file()
            )
            self.assertTrue(
                (target / "skills/agent/configure-test-workers/SKILL.md").is_file()
            )
            self.assertTrue(
                (target / "skills/agent/verify-frontend-browser/SKILL.md").is_file()
            )
            self.assertTrue((target / "skills/agent/ci-test-plan/SKILL.md").is_file())
            self.assertTrue(
                (target / "skills/agent/ci-test-plan/shard/SKILL.md").is_file()
            )
            self.assertTrue(
                (target / "skills/agent/ci-test-plan/impact/SKILL.md").is_file()
            )
            self.assertTrue(
                (target / "skills/agent/configure-operator-mcp/SKILL.md").is_file()
            )
            self.assertTrue((target / "skills/agent/SKILL.md").is_file())
            release_skill = target / "skills/agent/release-project/SKILL.md"
            self.assertTrue(release_skill.is_file())
            release_text = release_skill.read_text(encoding="utf-8")
            markdown_references = re.findall(r"`([^`\n]+\.md)`", release_text)
            self.assertTrue(markdown_references)
            for reference in markdown_references:
                with self.subTest(release_skill_reference=reference):
                    referenced_path = (
                        target / reference
                        if reference.startswith(("docs/", "skills/"))
                        else release_skill.parent / reference
                    )
                    self.assertTrue(
                        referenced_path.is_file(),
                        f"derived release skill reference is absent: {reference}",
                    )
            self.assertTrue(
                (target / "skills/agent/associate-release-jira/SKILL.md").is_file()
            )
            self.assertTrue(
                (target / "skills/agent/ingest-channel-work/SKILL.md").is_file()
            )
            self.assertTrue((target / "mcps").is_dir())
            self.assertTrue(
                (
                    target
                    / "skills/agent/develop-in-production-workflow/staging/dev/SKILL.md"
                ).is_file()
            )
            self.assertTrue(
                (target / "workflows/production/staging/dev/workflow.md").is_file()
            )
            self.assertTrue(
                (target / "skills/agent/write-mac-project-contract/SKILL.md").is_file()
            )
            self.assertTrue(
                (
                    target
                    / "skills/agent/write-mac-project-contract/references"
                    / "mac-repository-contract.md"
                ).is_file()
            )
            self.assertIn("/literate.test.json", (target / ".gitignore").read_text())
            self.assertIn("/literate.workers.json", (target / ".gitignore").read_text())
            self.assertIn(
                "/literate.worker-observations.json",
                (target / ".gitignore").read_text(),
            )
            self.assertIn("_build/", (target / ".gitignore").read_text())
            self.assertEqual(
                Path(result["test_matrix"]["configuration"]).name, "test.json"
            )
            self.assertEqual(
                Path(result["test_matrix"]["worker_configuration"]).name,
                "workers.json",
            )

            self.assertEqual(
                manifest["default_flavor_selectors"],
                [
                    "+flavor://literate-ai/build-bazel",
                    "+flavor://literate-ai/lang-python",
                    "+flavor://literate-ai/os-macos",
                ],
            )
            self.assertEqual(manifest["test_receipt"], "verification/current.json")
            self.assertTrue((target / "verification").is_dir())
            self.assertTrue(
                (target / "samples" / "hello-component" / "component.md").is_file()
            )
            self.assertTrue(
                (
                    target / "samples" / "hello-component" / "component.lock.json"
                ).is_file()
            )
            starter_lock = json.loads(
                (
                    target / "samples" / "hello-component" / "component.lock.json"
                ).read_text(encoding="utf-8")
            )
            selected_flavors = [
                selected["value"]
                for slot in starter_lock["nodes"][0]["target_flavor_selection"]["slots"]
                for selected in slot["selected"]
            ]
            self.assertEqual(
                selected_flavors,
                [
                    "bazel",
                    "python",
                    "macos",
                ],
            )
            self.assertIsInstance(result["initial_lock"], dict)
            self.assertIn("SKILL.md", result["created"])
            self.assertTrue(
                (
                    target
                    / "skills"
                    / "specification-to-source"
                    / "bazel-build-system"
                    / "SKILL.md"
                ).is_file()
            )
            self.assertTrue(
                (target / "skills" / "agent" / "release-project" / "SKILL.md").is_file()
            )
            artifact_skill = (
                target
                / "skills"
                / "agent"
                / "author-presentations-and-documents"
                / "SKILL.md"
            )
            self.assertTrue(artifact_skill.is_file())
            self.assertEqual(
                artifact_skill.read_bytes(),
                (
                    Path(__file__).resolve().parents[2]
                    / "skills"
                    / "agent"
                    / "author-presentations-and-documents"
                    / "SKILL.md"
                ).read_bytes(),
            )
            self.assertTrue(
                (target / "docs" / "architecture" / "design-traceability.md").is_file()
            )
            self.assertFalse((target / "flavors" / "doc-google-workspace").exists())
            self.assertFalse((target / "flavors" / "doc-microsoft-365").exists())

    def test_document_authoring_is_portable_and_publication_is_flavor_bound(
        self,
    ) -> None:
        repository = Path(__file__).resolve().parents[2]
        template = repository / "src" / "literate_ai" / "project_template"
        generic = (
            repository
            / "skills"
            / "agent"
            / "author-presentations-and-documents"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("No MCP is mandatory", generic)
        self.assertIn(
            "never substitute an internal smoke harness or `litai rebuild`", generic
        )
        self.assertIn("health/readiness checks", generic)
        self.assertNotIn("gcloud auth print-access-token", generic)
        self.assertEqual(
            generic,
            (
                template
                / "skills"
                / "agent"
                / "author-presentations-and-documents"
                / "SKILL.md"
            ).read_text(encoding="utf-8"),
        )

        google = repository / "flavors" / "doc-google-workspace" / "document-pair.md"
        microsoft = repository / "flavors" / "doc-microsoft-365" / "document-pair.md"
        self.assertIn(
            "gcloud auth print-access-token", google.read_text(encoding="utf-8")
        )
        self.assertIn(
            "device-code/browser login", microsoft.read_text(encoding="utf-8")
        )
        self.assertEqual(
            google.read_bytes(),
            (
                template / "flavors" / "doc-google-workspace" / "document-pair.md"
            ).read_bytes(),
        )
        self.assertEqual(
            microsoft.read_bytes(),
            (
                template / "flavors" / "doc-microsoft-365" / "document-pair.md"
            ).read_bytes(),
        )

    def test_initialized_project_packages_every_supported_language_closure(
        self,
    ) -> None:
        expected = {
            "python": (
                "python-portable-application",
                "python",
                "python-tree",
                "python",
            ),
            "javascript": (
                "javascript-portable-json-application",
                "node",
                "javascript-tree",
                "javascript",
            ),
            "rust": (
                "rust-portable-json-application",
                "rust",
                "rust-executable",
                "rust",
            ),
            "cpp": (
                "cpp17-portable-json-application",
                "cpp",
                "cpp-executable",
                "cpp",
            ),
            "swift": (
                "swift-portable-json-application",
                "swift",
                "swift-executable",
                "swift",
            ),
            "typescript": (
                "typescript-portable-application",
                "node",
                "typescript-tree",
                "lang-typescript",
            ),
            "zig": (
                "zig-portable-application",
                "zig",
                "zig-executable",
                "lang-zig",
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "language-closures"
            initialize_project(
                target,
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=(
                    "+python",
                    "+javascript",
                    "+rust",
                    "+cpp",
                    "+swift",
                    "+swift-apple",
                    "+typescript",
                    "+zig",
                    "+bazel",
                    "+macos",
                ),
            )
            validation = validate_project(
                target,
                require_authority_review=True,
                include_test_receipt=False,
                synchronize_source_intelligence=False,
            )

            for language, (
                skill_id,
                toolchain,
                build_strategy,
                _flavor_dir,
            ) in expected.items():
                with self.subTest(language=language):
                    flavor_path = target / "flavors" / f"lang-{language}" / "flavor.md"
                    flavor = parse_flavor_markdown(
                        flavor_path.read_bytes(), source=flavor_path.as_posix()
                    )
                    self.assertEqual(
                        (flavor.name, flavor.target), (f"lang-{language}", language)
                    )
                    self.assertEqual(len(flavor.authoring_inputs), 1)
                    self.assertEqual(
                        (
                            flavor.authoring_inputs[0].kind,
                            flavor.authoring_inputs[0].uri,
                        ),
                        (
                            "specification-to-source-skill",
                            f"../../skills/specification-to-source/{skill_id}/SKILL.md",
                        ),
                    )
                    language_profiles = [
                        item
                        for item in flavor.contributions
                        if item.slot == "standard-language-command"
                    ]
                    self.assertEqual(len(language_profiles), 1)
                    self.assertEqual(
                        language_profiles[0].content.uri,
                        "standard-command-profile.json",
                    )
                    profile_path = flavor_path.parent / "standard-command-profile.json"
                    profile = StandardLanguageCommandProfile.from_dict(
                        json.loads(profile_path.read_text(encoding="utf-8"))
                    )
                    self.assertEqual(profile.target, language)
                    self.assertEqual(profile.toolchain, toolchain)
                    self.assertEqual(profile.build_strategy.value, build_strategy)
                    self.assertEqual(
                        profile.cpp_library_kind,
                        "static" if language == "cpp" else None,
                    )
                    self.assertTrue(
                        (
                            target
                            / "skills"
                            / "specification-to-source"
                            / skill_id
                            / "SKILL.md"
                        ).is_file()
                    )

        validated_skills = {
            item["skill_id"] for item in validation["specification_to_source_skills"]
        }
        self.assertTrue(
            {skill_id for skill_id, _, _, _ in expected.values()}.issubset(
                validated_skills
            )
        )

    def test_empty_initialization_omits_only_the_starter_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "empty"

            result = initialize_project(
                target,
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos"),
            )

            self.assertNotIn("samples/hello-component/component.md", result["created"])
            self.assertIsNone(result["initial_lock"])
            self.assertFalse((target / "samples" / "hello-component").exists())
            self.assertTrue(
                (target / "flavors" / "lang-python" / "flavor.md").is_file()
            )

    def test_init_without_flavor_applies_smart_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "smart-defaults"
            result = initialize_project(target, source_intelligence_provider="none")
            # When called with no flavor_selectors the adapter uses its old defaults
            # (backward compat for the promote flow). CLI smart defaults are tested
            # through apply_init_flavor_defaults below.
            self.assertIn("SKILL.md", result["created"])
            self.assertTrue((target / "literate.project.json").is_file())

    def test_init_cli_applies_smart_defaults_when_no_flavor_given(self) -> None:
        selectors = apply_init_flavor_defaults([])

        self.assertIn("+flavor://literate-ai/lang-python", selectors)
        self.assertIn("+flavor://literate-ai/build-make", selectors)
        self.assertIn("+flavor://literate-ai/package-pip", selectors)
        self.assertIn(host_platform_selector(), selectors)
        self.assertNotIn("+flavor://literate-ai/build-bazel", selectors)

    def test_zip_selection_installs_provider_without_implicit_pip(self) -> None:
        selectors = apply_init_flavor_defaults(["package-zip"])
        self.assertIn("+flavor://literate-ai/package-zip", selectors)
        self.assertNotIn("+flavor://literate-ai/package-pip", selectors)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "zip-project"
            initialize_project(
                target,
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=selectors,
            )
            for name in ("flavor.md", "openspec/spec.md"):
                self.assertTrue((target / "flavors/package-zip" / name).is_file())
            self.assertFalse((target / "flavors/package-pip").exists())

    def test_two_language_flavors_default_to_package_conan(self) -> None:
        selectors = apply_init_flavor_defaults(["python", "javascript"])

        self.assertIn("+flavor://literate-ai/package-conan", selectors)
        self.assertNotIn("+flavor://literate-ai/package-pip", selectors)

    def test_init_cli_explicit_flavor_gets_os_added(self) -> None:
        selectors = apply_init_flavor_defaults(["javascript", "bazel"])

        self.assertIn("+flavor://literate-ai/lang-javascript", selectors)
        self.assertIn("+flavor://literate-ai/build-bazel", selectors)
        self.assertIn(host_platform_selector(), selectors)
        self.assertNotIn("+flavor://literate-ai/lang-python", selectors)
        self.assertNotIn("+flavor://literate-ai/build-make", selectors)

    def test_init_with_explicit_flavors_only_installs_matching_flavor_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "scoped"
            initialize_project(
                target,
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos"),
            )
            # Declared flavors are installed
            self.assertTrue(
                (target / "flavors" / "lang-python" / "flavor.md").is_file()
            )
            self.assertTrue((target / "flavors" / "os-macos" / "flavor.md").is_file())
            # Undeclared flavors are absent
            self.assertFalse((target / "flavors" / "lang-javascript").exists())
            self.assertFalse((target / "flavors" / "lang-rust").exists())
            self.assertFalse((target / "flavors" / "build-bazel").exists())
            self.assertFalse((target / "flavors" / "build-make").exists())
            self.assertFalse((target / "flavors" / "os-linux").exists())
            self.assertFalse((target / "flavors" / "deploy-docker").exists())
            self.assertFalse(
                (
                    target
                    / "skills"
                    / "specification-to-source"
                    / "docker-container-application"
                ).exists()
            )
            # Document-pair is absent without a service flavor
            self.assertFalse((target / "components" / "document-pair").exists())
            self.assertFalse(
                (
                    target / "skills" / "specification-to-source" / "document-pair"
                ).exists()
            )
            # Cross-cutting skills always present
            self.assertTrue(
                (
                    target
                    / "skills"
                    / "agent"
                    / "record-user-directed-work"
                    / "SKILL.md"
                ).is_file()
            )
            work_skill = (
                target / "skills" / "agent" / "record-user-directed-work" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn(
                "If a change applies\n  strictly to a parent Component",
                work_skill,
            )
            self.assertIn(
                "the proposed patch or local override and its work item",
                work_skill,
            )
            self.assertIn(
                "Keep the child's recovery,\n  mission vocabulary, paths, and release "
                "evidence downstream",
                work_skill,
            )
            # project.json has declared selectors
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["default_flavor_selectors"],
                [
                    "+flavor://literate-ai/lang-python",
                    "+flavor://literate-ai/os-macos",
                ],
            )

    def test_init_rejects_unknown_project_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "unknown-type"
            with self.assertRaises(ProjectInitializationError) as raised:
                initialize_project(
                    target,
                    source_intelligence_provider="none",
                    project_type="spaceship",
                )
            self.assertEqual(raised.exception.code, "project.init_type_unknown")

    def test_init_swift_adds_the_requested_os_toolchain_realization(self) -> None:
        self.assertEqual(
            apply_init_flavor_defaults(["lang-swift", "os-linux"]),
            (
                "+flavor://literate-ai/lang-swift",
                "+flavor://literate-ai/os-linux",
                "+flavor://literate-ai/build-make",
                "+flavor://literate-ai/package-pip",
                "+flavor://literate-ai/toolchain-swift-linux",
            ),
        )

    def test_init_locks_multiple_compatible_package_flavors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "multiple-packages"
            initialize_project(
                target,
                source_intelligence_provider="none",
                flavor_selectors=(
                    "+python",
                    "+make",
                    "+macos",
                    "+pip",
                    "+conan",
                ),
            )
            lock = json.loads(
                (target / "samples/hello-component/component.lock.json").read_text(
                    encoding="utf-8"
                )
            )
            slots = lock["nodes"][0]["target_flavor_selection"]["slots"]
            package_slot = next(
                item for item in slots if item["slot"]["axis"] == "packaging"
            )

            self.assertEqual(
                [item["value"] for item in package_slot["selected"]],
                ["conan", "pip"],
            )
            self.assertEqual(package_slot["slot"]["cardinality"], "bounded")
            self.assertEqual(package_slot["slot"]["minimum"], 0)
            self.assertEqual(package_slot["slot"]["maximum"], 6)

    def test_init_rejects_package_flavor_on_an_incompatible_host_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProjectInitializationError) as raised:
                initialize_project(
                    Path(directory) / "invalid-package-host",
                    source_intelligence_provider="none",
                    flavor_selectors=(
                        "+python",
                        "+make",
                        "+macos",
                        "+apt",
                    ),
                )

        self.assertEqual(
            raised.exception.code, "component_lock.flavor_target_unsatisfied"
        )
        self.assertIn("requires target value 'linux'", raised.exception.message)

    def test_swift_init_locks_one_compatible_realization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "swift-project"
            result = initialize_project(
                target,
                source_intelligence_provider="none",
                flavor_selectors=("+swift", "+macos", "+make", "+swift-apple"),
            )

            self.assertIsNotNone(result["initial_lock"])
            self.assertTrue(
                (
                    target / "samples" / "hello-component" / "component.lock.json"
                ).is_file()
            )
            self.assertEqual(
                sorted(path.name for path in (target / "flavors").iterdir()),
                [
                    "build-make",
                    "lang-swift",
                    "os-base",
                    "os-macos",
                    "toolchain-swift-apple",
                ],
            )

    def test_swift_init_rejects_an_os_toolchain_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProjectInitializationError) as raised:
                initialize_project(
                    Path(directory) / "incompatible",
                    source_intelligence_provider="none",
                    flavor_selectors=(
                        "+swift",
                        "+linux",
                        "+make",
                        "+swift-apple",
                    ),
                )

        self.assertEqual(
            raised.exception.code, "component_lock.flavor_target_unsatisfied"
        )

    def test_explicit_make_flavor_installs_only_its_build_system_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "make-project"
            initialize_project(
                target,
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos", "+make"),
            )

            flavor_path = target / "flavors" / "build-make" / "flavor.md"
            self.assertTrue(flavor_path.is_file())
            self.assertTrue(
                (target / "flavors/build-make/host-toolchain.cdx.json").is_file()
            )
            self.assertTrue((target / "flavors/os-base/toolchain.cdx.json").is_file())
            make_flavor = parse_flavor_markdown(
                flavor_path.read_bytes(), source=flavor_path.as_posix()
            )
            self.assertEqual(len(make_flavor.contributions), 1)
            self.assertEqual(
                make_flavor.contributions[0].content.uri,
                "standard-command-profile.json",
            )
            self.assertFalse(
                (
                    target
                    / "flavors"
                    / "build-make"
                    / "standard-make-command-profile.json"
                ).exists()
            )
            installed_skill = (
                target
                / "skills"
                / "specification-to-source"
                / "make-build-system"
                / "SKILL.md"
            )
            self.assertTrue(installed_skill.is_file())
            repository_skill = (
                Path(__file__).resolve().parents[2]
                / "skills"
                / "specification-to-source"
                / "make-build-system"
                / "SKILL.md"
            )
            self.assertEqual(
                installed_skill.read_bytes(), repository_skill.read_bytes()
            )
            installed_text = installed_skill.read_text(encoding="utf-8")
            self.assertIn(
                "## Generated application Makefile stays in source/", installed_text
            )
            self.assertIn("advisory cache prefixes", installed_text)
            self.assertNotIn("Always generate a thin root-level", installed_text)
            profile = json.loads(
                (
                    target / "flavors" / "build-make" / "standard-command-profile.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                profile,
                {
                    "schema": (
                        "urn:literate-ai:schema:v2:standard-make-command-profile"
                    ),
                    "target": "make",
                    "toolchain": "make",
                    "makefile": "source/Makefile",
                    "build_target": "all",
                },
            )
            self.assertFalse((target / "flavors" / "build-bazel").exists())
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["default_flavor_selectors"],
                [
                    "+flavor://literate-ai/lang-python",
                    "+flavor://literate-ai/os-macos",
                    "+flavor://literate-ai/build-make",
                ],
            )

    def test_explicit_cargo_flavor_installs_its_complete_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cargo-project"
            initialize_project(
                target,
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+rust", "+linux", "+cargo"),
            )

            profile = json.loads(
                (
                    target / "flavors/build-cargo/standard-command-profile.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(profile["toolchain"], "cargo")
            self.assertEqual(profile["manifest"], "source/Cargo.toml")
            self.assertEqual(profile["binary"], "litai_artifact")
            for skill in ("cargo-build-system", "rust-ecosystem"):
                self.assertTrue(
                    (
                        target
                        / "skills"
                        / "specification-to-source"
                        / skill
                        / "SKILL.md"
                    ).is_file()
                )
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["default_flavor_selectors"],
                [
                    "+flavor://literate-ai/lang-rust",
                    "+flavor://literate-ai/os-linux",
                    "+flavor://literate-ai/build-cargo",
                ],
            )

    def test_nonempty_init_needs_language_and_os_but_not_a_build_system(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            incomplete = Path(directory) / "incomplete"
            with self.assertRaises(ProjectInitializationError) as raised:
                initialize_project(
                    incomplete,
                    source_intelligence_provider="none",
                    flavor_selectors=("+python",),
                )
            self.assertEqual(raised.exception.code, "project.init_flavor_axis_required")
            self.assertFalse(incomplete.exists())

            target = Path(directory) / "native-build"
            result = initialize_project(
                target,
                source_intelligence_provider="none",
                flavor_selectors=("+python", "+macos"),
            )
            self.assertIsInstance(result["initial_lock"], dict)
            self.assertTrue(
                (
                    target / "samples" / "hello-component" / "component.lock.json"
                ).is_file()
            )
            self.assertFalse((target / "flavors" / "build-bazel").exists())

    def test_init_unknown_flavor_exits_with_diagnostic(self) -> None:
        from argparse import Namespace

        from literate_ai.cli.errors import CliFailure
        from literate_ai.cli.project import init_project_from_args

        args = Namespace(
            path="project",
            project_id=None,
            profile="canonical",
            source_intelligence_provider="none",
            empty=False,
            no_tool_bootstrap=True,
            flavors=["openscad"],  # not a known flavor
        )
        with self.assertRaises(CliFailure) as raised:
            init_project_from_args(args)
        self.assertEqual(raised.exception.code, "project.init_flavor_unknown")
        self.assertIn("openscad", raised.exception.message)


class ConvertModeTests(unittest.TestCase):
    @staticmethod
    def _origin() -> ProjectInitializationOrigin:
        return ProjectInitializationOrigin(
            repository_url="ssh://git.example.test/operator/literate-ai.git",
            git_revision="b" * 40,
            distribution_name="literate-ai",
            distribution_version="0.2.0",
        )

    def test_convert_rejects_invalid_baseline_timeout_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing-project"
            target.mkdir()
            source = target / "Makefile"
            source.write_text("all:\n\t@true\n", encoding="utf-8")

            with self.assertRaises(ProjectInitializationError) as raised:
                FilesystemProjectInitializationAdapter(
                    initialization_origin_provider=lambda: self._origin(),
                    standard_binding_provider=lambda: None,
                ).initialize(
                    target,
                    source_intelligence_provider="none",
                    convert=True,
                    baseline_timeout_seconds=0,
                )

            self.assertEqual(raised.exception.code, "project.convert_timeout_invalid")
            self.assertTrue(source.is_file())
            self.assertFalse((target / "_legacy").exists())

    def test_convert_parent_preflight_failure_leaves_repository_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing-project"
            target.mkdir()
            tracked = target / "Makefile"
            tracked.write_text("all:\n\t@true\ntest:\n\t@true\n", encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet"),
                check=True,
                capture_output=True,
            )
            for key, value in (
                ("user.email", "test@example.test"),
                ("user.name", "Test"),
            ):
                subprocess.run(
                    ("git", "-C", str(target), "config", key, value),
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ("git", "-C", str(target), "add", "Makefile"),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "commit", "--quiet", "-m", "baseline"),
                check=True,
                capture_output=True,
            )
            untracked = target / "operator-notes.txt"
            untracked.write_text("preserve me\n", encoding="utf-8")
            tracked_before = tracked.read_bytes()
            untracked_before = untracked.read_bytes()

            status_before = subprocess.run(
                ("git", "-C", str(target), "status", "--porcelain=v1", "-z"),
                check=True,
                capture_output=True,
            ).stdout
            with mock.patch(
                "literate_ai.adapters.project_initialization."
                "_default_repository_parent",
                side_effect=ProjectInitializationError(
                    "repository_lineage.tags_unavailable",
                    "framework release tags are unavailable",
                ),
            ):
                with self.assertRaises(ProjectInitializationError) as raised:
                    DefaultParentProjectInitializationAdapter(
                        initialization_origin_provider=lambda: self._origin(),
                        standard_binding_provider=lambda: None,
                    ).initialize(
                        target,
                        source_intelligence_provider="none",
                        convert=True,
                    )

            status_after = subprocess.run(
                ("git", "-C", str(target), "status", "--porcelain=v1", "-z"),
                check=True,
                capture_output=True,
            ).stdout
            self.assertEqual(
                raised.exception.code, "repository_lineage.tags_unavailable"
            )
            self.assertEqual(status_after, status_before)
            self.assertEqual(tracked.read_bytes(), tracked_before)
            self.assertEqual(untracked.read_bytes(), untracked_before)
            self.assertFalse((target / "_legacy").exists())
            self.assertFalse((target / "literate.project.json").exists())

    def test_convert_quarantines_existing_tree_into_unique_aside_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing-project"
            target.mkdir()
            legacy_layout = {
                "AGENTS.md": "# Project agents\n",
                "CHANGELOG.md": "# My changelog\n",
                ".gitignore": "build/\n*.pyc\n",
                "skills/config.yaml": "schema: 1\n",
                "src/main.py": "print('hello')\n",
                "docs/notes/design.md": "# Design\n",
            }
            for relative, content in sorted(legacy_layout.items()):
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet"),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "add", "-A"),
                check=True,
                capture_output=True,
            )
            for arguments in (
                ("config", "user.email", "test@example.test"),
                ("config", "user.name", "Test"),
            ):
                subprocess.run(
                    ("git", "-C", str(target), *arguments),
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ("git", "-C", str(target), "commit", "--quiet", "-m", "baseline"),
                check=True,
                capture_output=True,
                env={**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z"},
            )

            result = FilesystemProjectInitializationAdapter(
                initialization_origin_provider=lambda: self._origin(),
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )

            # Phase 0 recorded one exact quarantine; Phase 1.2 subsequently moved
            # that hierarchy intact into first-class Component authority and removed
            # the temporary _legacy directory only after parity passed.
            quarantine = result["convert_quarantine"]
            self.assertIsNotNone(quarantine)
            aside = target / quarantine["directory"]
            self.assertFalse(aside.exists())
            self.assertTrue(result["lift_shift"]["quarantine_removed"])
            implementation = target / result["lift_shift"]["implementation_directory"]
            self.assertTrue(implementation.is_dir())
            self.assertEqual(
                quarantine["directory"].split("/")[0],
                "_legacy",
            )
            self.assertEqual(len(quarantine["moved"]), len(legacy_layout))
            moved_names = {item["from"] for item in quarantine["moved"]}
            self.assertEqual(
                moved_names,
                {"AGENTS.md", "CHANGELOG.md", ".gitignore", "skills", "src", "docs"},
            )
            for relative, content in legacy_layout.items():
                self.assertEqual(
                    (implementation / relative).read_text(encoding="utf-8"), content
                )
            # Operator-owned entries must not reappear at the root; scaffold-managed
            # names (AGENTS.md, .gitignore, CHANGELOG.md, skills/, docs/) are freshly
            # stamped from the template.
            for relative in ("src", "skills/config.yaml", "docs/notes/design.md"):
                self.assertFalse((target / relative).exists())
            self.assertTrue((target / ".gitignore").is_file())
            stamped = (target / ".gitignore").read_text()
            self.assertNotIn("build/", stamped.replace("_build/", ""))
            self.assertNotIn("*.pyc", stamped)
            self.assertNotEqual(
                (target / "CHANGELOG.md").read_text(encoding="utf-8"),
                "# My changelog\n",
            )

            # Git-tracked entries were moved with git mv (rename detection applies).
            git_vcs = {item["from"]: item["vcs"] for item in quarantine["moved"]}
            self.assertEqual(git_vcs["src"], "git")
            status = subprocess.run(
                ("git", "-C", str(target), "status", "--porcelain", "-M"),
                check=True,
                capture_output=True,
                text=True,
            )
            renamed = [
                line
                for line in status.stdout.splitlines()
                if line.startswith("R  ") or line.startswith("R ")
            ]
            self.assertTrue(renamed)

            # The scaffold stamped a clean tree: no operator content remains at root.
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertFalse((target / "samples" / "hello-component").exists())
            self.assertNotIn(".git", moved_names)
            self.assertTrue((target / ".git").is_dir())
            self.assertEqual(result["detected_languages"], ["python"])

    def test_convert_prefers_current_upstream_and_guidance_uses_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            remote = root / "remote.git"
            target = root / "converted"
            source.mkdir()
            subprocess.run(
                ("git", "-C", str(source), "init", "--quiet", "-b", "master"),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(source), "config", "user.email", "test@example.test"),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(source), "config", "user.name", "Test"),
                check=True,
            )
            (source / "Makefile").write_text(
                "all:\n\t@true\ntest:\n\t@true\n", encoding="utf-8"
            )
            subprocess.run(("git", "-C", str(source), "add", "Makefile"), check=True)
            subprocess.run(
                ("git", "-C", str(source), "commit", "--quiet", "-m", "initial"),
                check=True,
            )
            subprocess.run(
                ("git", "clone", "--bare", str(source), str(remote)), check=True
            )
            subprocess.run(("git", "clone", str(remote), str(target)), check=True)

            planned = plan_convert(target)
            self.assertEqual(
                planned["repository_default_branch"],
                {"branch": "master", "source": "current-upstream"},
            )
            self.assertEqual(planned["landing_stage"], "wrapped")
            self.assertEqual(
                planned["retained_runners"],
                [stage["id"] for stage in planned["proposed_wrapper_stages"]],
            )
            self.assertEqual(planned["gitlink_blockers"], [])
            self.assertIn("Makefile", planned["quarantine"]["entries"])
            converted = FilesystemProjectInitializationAdapter(
                initialization_origin_provider=lambda: self._origin(),
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )

            authority_path = target / ".literate" / "conversion-authority.json"
            self.assertTrue(authority_path.is_file())
            self.assertEqual(converted["conversion_authority"]["stage"], "wrapped")
            self.assertEqual(
                json.loads(authority_path.read_text(encoding="utf-8"))["stage"],
                "wrapped",
            )

            manifest = ProjectDefinition.from_dict(
                json.loads((target / "literate.project.json").read_text())
            )
            self.assertEqual(manifest.repository_policy.default_branch, "master")
            guidance = project_guidance(target, operation="develop")
            self.assertEqual(
                guidance["facts"]["configuration"]["default_branch"], "master"
            )

    def test_convert_with_ambiguous_remote_requires_explicit_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "ambiguous"
            remote = root / "empty.git"
            target.mkdir()
            subprocess.run(("git", "init", "--bare", str(remote)), check=True)
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet", "-b", "main"),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "config", "user.email", "test@example.test"),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "config", "user.name", "Test"),
                check=True,
            )
            (target / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(target), "add", "Makefile"), check=True)
            subprocess.run(
                ("git", "-C", str(target), "commit", "--quiet", "-m", "initial"),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "remote", "add", "origin", str(remote)),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "checkout", "--quiet", "--detach"),
                check=True,
            )

            with self.assertRaises(ProjectInitializationError) as raised:
                plan_convert(target)
            self.assertEqual(
                raised.exception.code, "project.convert_default_branch_unresolved"
            )
            self.assertEqual(
                plan_convert(target, default_branch="main")[
                    "repository_default_branch"
                ],
                {"branch": "main", "source": "cli"},
            )

    def test_cuda_skill_template_includes_openai_agent_shim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cuda-shim"
            initialize_project(
                target,
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos", "+accel-nvidia-cuda"),
            )
            shim = (
                target
                / "skills"
                / "specification-to-source"
                / "generate-nvidia-cuda-application"
                / "agents"
                / "openai.yaml"
            )
            self.assertTrue(shim.is_file())

    def test_convert_without_git_uses_plain_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "plain-project"
            target.mkdir()
            (target / "README.md").write_text("# Legacy\n", encoding="utf-8")
            (target / "lib").mkdir()
            (target / "lib" / "core.py").write_text("x = 1\n", encoding="utf-8")

            result = FilesystemProjectInitializationAdapter(
                initialization_origin_provider=lambda: self._origin(),
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )

            quarantine = result["convert_quarantine"]
            self.assertIsNotNone(quarantine)
            self.assertFalse((target / quarantine["directory"]).exists())
            implementation = target / result["lift_shift"]["implementation_directory"]
            self.assertTrue((implementation / "README.md").is_file())
            self.assertTrue((implementation / "lib" / "core.py").is_file())
            self.assertEqual({item["vcs"] for item in quarantine["moved"]}, {"fs"})
            self.assertTrue((target / "literate.project.json").is_file())

    def test_convert_mixed_tracked_and_untracked_entries_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "mixed-project"
            target.mkdir()
            (target / "tracked.py").write_text("a = 1\n", encoding="utf-8")
            (target / "untracked.py").write_text("b = 2\n", encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet"),
                check=True,
                capture_output=True,
            )
            for arguments in (
                ("config", "user.email", "test@example.test"),
                ("config", "user.name", "Test"),
            ):
                subprocess.run(
                    ("git", "-C", str(target), *arguments),
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ("git", "-C", str(target), "add", "tracked.py"),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "commit", "--quiet", "-m", "one file"),
                check=True,
                capture_output=True,
            )

            result = FilesystemProjectInitializationAdapter(
                initialization_origin_provider=lambda: self._origin(),
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )

            quarantine = result["convert_quarantine"]
            vcs = {item["from"]: item["vcs"] for item in quarantine["moved"]}
            self.assertEqual(vcs["tracked.py"], "git")
            self.assertEqual(vcs["untracked.py"], "fs")

    def test_convert_refuses_root_symlink_escaping_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside"
            outside.mkdir()
            target = Path(directory) / "linked-project"
            target.mkdir()
            (target / "escape").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ProjectInitializationError) as raised:
                FilesystemProjectInitializationAdapter(
                    initialization_origin_provider=lambda: self._origin(),
                    standard_binding_provider=lambda: None,
                ).initialize(
                    target,
                    source_intelligence_provider="none",
                    convert=True,
                )

            self.assertEqual(raised.exception.code, "project.convert_unsafe_entry")
            self.assertFalse((target / "literate.project.json").exists())

    def test_convert_already_initialized_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "already-litai"
            # First init
            FilesystemProjectInitializationAdapter(
                initialization_origin_provider=lambda: self._origin(),
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                source_intelligence_provider="none",
                empty=True,
            )
            # Second --convert attempt
            with self.assertRaises(ProjectInitializationError) as raised:
                FilesystemProjectInitializationAdapter(
                    initialization_origin_provider=lambda: self._origin(),
                    standard_binding_provider=lambda: None,
                ).initialize(
                    target,
                    source_intelligence_provider="none",
                    convert=True,
                )
            self.assertEqual(raised.exception.code, "project.already_initialized")
            self.assertIn("does not require conversion", raised.exception.message)
            self.assertIn("litai update", raised.exception.message)

    def test_convert_resumes_interrupted_scaffold_without_requarantine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "partial-convert"
            target.mkdir()
            (target / ".literate").mkdir()
            (target / ".literate" / "initialization-baseline.json").write_text(
                "{}\n", encoding="utf-8"
            )
            leftover = target / "legacy-module.py"
            leftover.write_text("VALUE = 1\n", encoding="utf-8")
            (target / "Makefile").write_text(
                "all:\n\t@true\ntest:\n\t@true\n", encoding="utf-8"
            )
            FilesystemProjectInitializationAdapter(
                initialization_origin_provider=lambda: self._origin(),
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertTrue(leftover.is_file())
            self.assertFalse((target / "_legacy").exists())

    def test_detect_repo_flavors_identifies_python_and_make(self) -> None:
        from literate_ai.adapters.project_initialization import detect_repo_flavors

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "Makefile").write_text(
                "all:\n\tpython3 main.py\n", encoding="utf-8"
            )
            selectors = detect_repo_flavors(root)
            self.assertIn("flavor://literate-ai/lang-python", selectors)
            self.assertIn("flavor://literate-ai/build-make", selectors)
            self.assertNotIn("flavor://literate-ai/lang-rust", selectors)
            self.assertNotIn("flavor://literate-ai/lang-javascript", selectors)

    def test_detect_repo_flavors_picks_cpp_when_python_is_also_present(self) -> None:
        from literate_ai.adapters.project_initialization import (
            detect_repo_flavors,
            detected_language_flavors,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            selectors = detect_repo_flavors(root)
            self.assertIn("flavor://literate-ai/lang-cpp", selectors)
            self.assertNotIn("flavor://literate-ai/lang-python", selectors)
            self.assertEqual(
                detected_language_flavors(root),
                [
                    "flavor://literate-ai/lang-cpp",
                    "flavor://literate-ai/lang-python",
                ],
            )

    def test_detect_repo_flavors_identifies_rust_and_cargo(self) -> None:
        from literate_ai.adapters.project_initialization import detect_repo_flavors

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cargo.toml").write_text(
                '[package]\nname = "service"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )

            selectors = detect_repo_flavors(root)

            self.assertIn("flavor://literate-ai/lang-rust", selectors)
            self.assertIn("flavor://literate-ai/build-cargo", selectors)
            self.assertNotIn("flavor://literate-ai/build-make", selectors)


if __name__ == "__main__":
    unittest.main()
