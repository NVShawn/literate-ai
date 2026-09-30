from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import literate_ai
from literate_ai.adapters.standard_lifecycle_binding import (
    StandardLifecycleBindingError,
)
from literate_ai.cli import main
from literate_ai.contracts import StandardProjectLifecycleDriver, canonical_identity
from literate_ai.schema_catalog import (
    SCHEMA_CATALOG_ROOT_ENVIRONMENT,
    SchemaCatalogError,
    schema_catalog_root,
)
from literate_ai.version import DISTRIBUTION_VERSION, EXPECTED_RELEASE_TAG
from literate_ai.version_check import _git_state, _project_state, check_versions

REPOSITORY = Path(__file__).resolve().parents[2]


class VersionAuthorityTests(unittest.TestCase):
    def test_standard_project_binding_uses_the_installed_binding_resolver(self) -> None:
        driver = StandardProjectLifecycleDriver(
            canonical_identity({"fixture": "installed-distribution"}),
            canonical_identity({"fixture": "standard-policy"}),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "literate.project.json").write_text("{}\n", encoding="utf-8")
            project = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(
                    SCHEMA="urn:literate-ai:schema:v2:project-definition",
                    project_id="standard-project",
                    version="1.0.0",
                    lifecycle_driver=driver,
                    test_receipt_policy=None,
                    component_roots=(),
                ),
            )
            with (
                patch(
                    "literate_ai.version_check.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.version_check."
                    "resolve_standard_project_lifecycle_driver"
                ) as resolve,
            ):
                report, selected = _project_state(root)

        resolve.assert_called_once_with(driver)
        self.assertEqual(selected, root)
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["lifecycle_driver"],
            {
                "schema": driver.SCHEMA,
                "binding": "standard",
                "framework_distribution_identity": (
                    driver.framework_distribution_identity.uri
                ),
                "policy_identity": driver.policy_identity.uri,
            },
        )
        self.assertTrue(report["derived_identities"]["driver_identity_matches"])
        self.assertIsNone(report["derived_identities"]["driver_resolution_error"])

    def test_standard_project_binding_reports_resolution_mismatch(self) -> None:
        driver = StandardProjectLifecycleDriver(
            canonical_identity({"fixture": "configured-distribution"}),
            canonical_identity({"fixture": "standard-policy"}),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "literate.project.json").write_text("{}\n", encoding="utf-8")
            project = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(
                    SCHEMA="urn:literate-ai:schema:v2:project-definition",
                    project_id="standard-project",
                    version="1.0.0",
                    lifecycle_driver=driver,
                    test_receipt_policy=None,
                    component_roots=(),
                ),
            )
            mismatch = StandardLifecycleBindingError(
                "standard_binding.distribution_mismatch",
                "installed distribution differs",
            )
            with (
                patch(
                    "literate_ai.version_check.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.version_check."
                    "resolve_standard_project_lifecycle_driver",
                    side_effect=mismatch,
                ),
            ):
                report, _ = _project_state(root)

        self.assertFalse(report["ok"])
        self.assertFalse(report["derived_identities"]["driver_identity_matches"])
        self.assertEqual(
            report["derived_identities"]["driver_resolution_error"],
            {
                "code": "standard_binding.distribution_mismatch",
                "message": "installed distribution differs",
            },
        )

    def test_distribution_metadata_is_derived_from_one_python_authority(self) -> None:
        configuration = tomllib.loads(
            (REPOSITORY / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertRegex(DISTRIBUTION_VERSION, r"^\d+\.\d+\.\d+")
        self.assertEqual(literate_ai.__version__, DISTRIBUTION_VERSION)
        self.assertEqual(EXPECTED_RELEASE_TAG, f"v{DISTRIBUTION_VERSION}")
        self.assertEqual(configuration["project"]["dynamic"], ["version"])
        self.assertNotIn("version", configuration["project"])
        self.assertEqual(
            configuration["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "literate_ai.version.DISTRIBUTION_VERSION"},
        )

    def test_unknown_schema_catalog_versions_fail_closed(self) -> None:
        with self.assertRaises(SchemaCatalogError) as raised:
            schema_catalog_root("v3")
        self.assertEqual(raised.exception.code, "schema.catalog_version_unsupported")

    def test_explicit_schema_catalog_root_is_complete_absolute_and_authoritative(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "embedded-catalog"
            shutil.copytree(REPOSITORY / "schemas", catalog)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "literate_ai.schema_catalog.sysconfig.get_path",
                    return_value=str(root / "unrelated-install"),
                ),
            ):
                self.assertEqual(
                    schema_catalog_root("v1"),
                    (REPOSITORY / "schemas" / "v1").resolve(),
                )

            with (
                patch.dict(
                    os.environ,
                    {SCHEMA_CATALOG_ROOT_ENVIRONMENT: str(catalog)},
                ),
                patch(
                    "literate_ai.schema_catalog.sysconfig.get_path",
                    return_value=str(root / "unrelated-install"),
                ),
            ):
                self.assertEqual(schema_catalog_root("v1"), (catalog / "v1").resolve())
                self.assertEqual(schema_catalog_root("v2"), (catalog / "v2").resolve())

            with patch.dict(
                os.environ,
                {SCHEMA_CATALOG_ROOT_ENVIRONMENT: "relative/catalog"},
            ):
                with self.assertRaises(SchemaCatalogError) as raised:
                    schema_catalog_root("v1")
                self.assertEqual(
                    raised.exception.code,
                    "schema.catalog_root_configuration_invalid",
                )

            shutil.rmtree(catalog / "v2")
            with patch.dict(
                os.environ,
                {SCHEMA_CATALOG_ROOT_ENVIRONMENT: str(catalog)},
            ):
                with self.assertRaises(SchemaCatalogError) as raised:
                    schema_catalog_root("v1")
                self.assertEqual(
                    raised.exception.code,
                    "schema.catalog_version_missing",
                )

    @unittest.skipIf(os.name == "nt", "directory symlink creation is privileged")
    def test_explicit_schema_catalog_root_rejects_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog"
            shutil.copytree(REPOSITORY / "schemas", catalog)
            linked = root / "linked-catalog"
            linked.symlink_to(catalog, target_is_directory=True)
            with patch.dict(
                os.environ,
                {SCHEMA_CATALOG_ROOT_ENVIRONMENT: str(linked)},
            ):
                with self.assertRaises(SchemaCatalogError) as raised:
                    schema_catalog_root("v1")
                self.assertEqual(raised.exception.code, "schema.catalog_root_invalid")

    def test_cli_version_check_covers_authority_and_both_catalogs(self) -> None:
        output = StringIO()
        errors = StringIO()
        status = main(
            ["version", "check", "--no-project"],
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(status, 0, errors.getvalue())
        envelope = json.loads(output.getvalue())
        self.assertTrue(envelope["ok"])
        result = envelope["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["distribution"]["authority_version"], DISTRIBUTION_VERSION
        )
        self.assertEqual(set(result["schema_catalogs"]), {"v1", "v2"})
        self.assertEqual(result["schema_catalogs"]["v1"]["published"]["file_count"], 17)
        self.assertEqual(
            result["schema_catalogs"]["v2"]["release"], DISTRIBUTION_VERSION
        )

    def test_stale_editable_metadata_is_explicit_not_a_source_regression(self) -> None:
        with (
            patch(
                "literate_ai.version_check._distribution_metadata_version",
                return_value="0.8.3",
            ),
            patch(
                "literate_ai.version_check._distribution_is_editable",
                return_value=True,
            ),
        ):
            report = check_versions(project_path=None)

        distribution = report["distribution"]
        self.assertTrue(report["ok"])
        self.assertTrue(distribution["ok"])
        self.assertFalse(distribution["metadata_matches_authority"])
        self.assertEqual(distribution["install_kind"], "editable")
        self.assertEqual(
            distribution["diagnostic"]["code"],
            "distribution.editable_metadata_stale",
        )

    def test_non_editable_metadata_mismatch_remains_strict(self) -> None:
        with (
            patch(
                "literate_ai.version_check._distribution_metadata_version",
                return_value="0.8.3",
            ),
            patch(
                "literate_ai.version_check._distribution_is_editable",
                return_value=False,
            ),
        ):
            report = check_versions(project_path=None)

        self.assertFalse(report["ok"])
        self.assertFalse(report["distribution"]["ok"])
        self.assertIsNone(report["distribution"]["diagnostic"])

    def test_self_hosting_project_protocols_and_derived_identities_agree(self) -> None:
        report = check_versions(project_path=REPOSITORY)
        self.assertTrue(report["ok"], report)
        project = report["project"]
        self.assertEqual(project["state"], "loaded")
        self.assertEqual(project["project_version"], DISTRIBUTION_VERSION)
        self.assertEqual(project["lifecycle_driver"]["version"], DISTRIBUTION_VERSION)
        self.assertEqual(project["test_suite"]["suite_version"], DISTRIBUTION_VERSION)
        self.assertTrue(project["derived_identities"]["driver_identity_matches"])
        self.assertTrue(project["derived_identities"]["runner_identity_matches"])
        self.assertGreaterEqual(project["component_contracts"]["count"], 1)
        self.assertEqual(report["tag"]["expected_tag"], EXPECTED_RELEASE_TAG)

    def test_release_mode_requires_the_exact_version_tag(self) -> None:
        report = check_versions(
            project_path=REPOSITORY,
            require_release_tag=True,
        )
        if EXPECTED_RELEASE_TAG in report["tag"]["tags_at_head"]:
            self.assertTrue(report["tag"]["ok"])
        else:
            self.assertFalse(report["ok"])
            self.assertFalse(report["tag"]["ok"])

    def test_release_tag_is_discovered_from_nested_and_linked_worktrees(self) -> None:
        def git(root: Path, *args: str) -> None:
            subprocess.run(
                ["git", "-C", str(root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            repository = parent / "repository"
            repository.mkdir()
            git(repository, "init")
            git(repository, "config", "user.email", "version-test@example.invalid")
            git(repository, "config", "user.name", "Version Test")
            (repository / "authority.txt").write_text("v0.2.0\n", encoding="utf-8")
            git(repository, "add", "authority.txt")
            git(repository, "commit", "-m", "version fixture")
            git(repository, "tag", EXPECTED_RELEASE_TAG)

            nested = repository / "projects" / "nested"
            nested.mkdir(parents=True)
            self.assertEqual(
                _git_state(nested, require_release_tag=True)["state"], "released"
            )

            linked = parent / "linked-worktree"
            git(repository, "worktree", "add", "--detach", str(linked), "HEAD")
            linked_nested = linked / "projects" / "nested"
            linked_nested.mkdir(parents=True)
            linked_report = _git_state(linked_nested, require_release_tag=True)
            self.assertEqual(linked_report["state"], "released")
            self.assertTrue(linked_report["ok"])


if __name__ == "__main__":
    unittest.main()
