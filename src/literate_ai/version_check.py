"""Read-only consistency check for release, protocol, and derived identities."""

from __future__ import annotations

import json
import os
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.component_markdown import (
    ComponentMarkdownError,
    parse_component_markdown,
)
from literate_ai.adapters.project_lifecycle_driver import (
    lifecycle_driver_implementation_identity,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    StandardLifecycleBindingError,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.contracts import SemanticVersion, StandardProjectLifecycleDriver
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project
from literate_ai.schema_catalog import (
    SCHEMA_CATALOG_ROOT_ENVIRONMENT,
    SUPPORTED_SCHEMA_CATALOGS,
    PublishedSchemaError,
    SchemaCatalogError,
    schema_catalog_root,
    verify_published_schemas,
    verify_schema_catalog,
)
from literate_ai.test_runner_authority import (
    SAMPLE_TEST_RUNNER_SOURCE_PATHS,
    sample_test_runner_source_closure_identity,
)
from literate_ai.version import (
    DISTRIBUTION_VERSION,
    EXPECTED_RELEASE_TAG,
    LIFECYCLE_DRIVER_SCHEMA,
    PROJECT_PROTOCOL_SCHEMA,
    SCHEMA_CATALOG_RELEASES,
    SUPPORTED_COMPONENT_CONTRACT_SCHEMAS,
    TEST_SUITE_POLICY_SCHEMA,
)

VERSION_CHECK_SCHEMA = "literate-ai/version-check@1"


class VersionCheckError(RuntimeError):
    """Version checking could not safely inspect its requested scope."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _distribution_metadata_version() -> str | None:
    try:
        return metadata.version("literate-ai")
    except metadata.PackageNotFoundError:
        return None


def _distribution_is_editable() -> bool:
    """Return whether installer metadata binds this process to a working tree."""

    try:
        raw = metadata.distribution("literate-ai").read_text("direct_url.json")
    except (metadata.PackageNotFoundError, OSError):
        return False
    if raw is None:
        return False
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(
        isinstance(document, dict)
        and isinstance(document.get("dir_info"), dict)
        and document["dir_info"].get("editable") is True
    )


def _schema_root(version: str, project_root: Path | None) -> Path:
    if SCHEMA_CATALOG_ROOT_ENVIRONMENT in os.environ:
        return schema_catalog_root(version)
    if project_root is not None:
        candidate = project_root / "schemas" / version
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate
    source_candidate = Path(__file__).resolve().parents[2] / "schemas" / version
    if source_candidate.is_dir() and not source_candidate.is_symlink():
        return source_candidate
    return schema_catalog_root(version)


def _git_state(root: Path | None, *, require_release_tag: bool) -> dict[str, object]:
    report: dict[str, object] = {
        "expected_tag": EXPECTED_RELEASE_TAG,
        "head": None,
        "tags_at_head": [],
        "state": "unavailable",
        "ok": not require_release_tag,
    }
    if root is None:
        return report
    try:
        inside_worktree = run_with_tree_kill(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            timeout=10,
            text=True,
            check=True,
        ).stdout.strip()
        if inside_worktree != "true":
            return report
        head = run_with_tree_kill(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            timeout=10,
            text=True,
            check=True,
        ).stdout.strip()
        tags = tuple(
            sorted(
                run_with_tree_kill(
                    ["git", "-C", str(root), "tag", "--points-at", "HEAD"],
                    timeout=10,
                    text=True,
                    check=True,
                ).stdout.splitlines()
            )
        )
    except (OSError, subprocess.SubprocessError):
        return report
    report["head"] = head
    report["tags_at_head"] = list(tags)
    if EXPECTED_RELEASE_TAG in tags:
        report["state"] = "released"
        report["ok"] = True
    elif tags:
        report["state"] = "mismatched-release-tag"
        report["ok"] = False
    else:
        report["state"] = "unreleased"
        report["ok"] = not require_release_tag
    return report


def _component_contracts(project) -> dict[str, object]:
    manifests: list[dict[str, str]] = []
    ok = True
    for relative_root in project.definition.component_roots:
        root = project.root.joinpath(*Path(relative_root).parts)
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            ok = False
            continue
        for path in sorted(root.rglob("component.md")):
            if path.is_symlink() or not path.is_file():
                ok = False
                continue
            try:
                authoring = parse_component_markdown(
                    path,
                    path.read_text(encoding="utf-8"),
                    project_root=project.root,
                )
            except (ComponentMarkdownError, OSError, UnicodeError, ValueError):
                ok = False
                continue
            manifests.append(
                {
                    "path": path.relative_to(project.root).as_posix(),
                    "schema": authoring.SCHEMA,
                    "component_version": authoring.version,
                }
            )
        for path in sorted(root.rglob("component.json")):
            if path.with_name("component.md").is_file():
                continue
            if path.is_symlink() or not path.is_file():
                ok = False
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                schema = value["schema"]
                version = value["version"]
                if (
                    schema not in SUPPORTED_COMPONENT_CONTRACT_SCHEMAS
                    or str(SemanticVersion.parse(version)) != version
                ):
                    raise ValueError
            except (KeyError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
                ok = False
                continue
            manifests.append(
                {
                    "path": path.relative_to(project.root).as_posix(),
                    "schema": schema,
                    "component_version": version,
                }
            )
    return {
        "supported_schemas": sorted(
            {
                *SUPPORTED_COMPONENT_CONTRACT_SCHEMAS,
                "urn:literate-ai:schema:v2:component-authoring",
            }
        ),
        "manifests": manifests,
        "count": len(manifests),
        "ok": ok,
    }


def _project_state(project_path: Path | None) -> tuple[dict[str, object], Path | None]:
    unavailable: dict[str, object] = {
        "state": "unavailable",
        "project_id": None,
        "protocol_schema": None,
        "project_version": None,
        "lifecycle_driver": None,
        "test_suite": None,
        "component_contracts": None,
        "derived_identities": None,
        "ok": True,
    }
    if project_path is None:
        return unavailable, None
    configured = Path(project_path)
    if configured.is_symlink():
        raise VersionCheckError(
            "version.project_root_invalid", "project root must not be a symbolic link"
        )
    try:
        selected = configured.resolve(strict=True)
    except OSError as exc:
        raise VersionCheckError(
            "version.project_root_invalid", "project root is unavailable"
        ) from exc
    if not (selected / PROJECT_FILENAME).is_file():
        return unavailable, selected
    try:
        project = discover_project(selected)
    except ProjectError as exc:
        raise VersionCheckError(exc.code, str(exc)) from exc
    if project is None:
        raise VersionCheckError(
            "version.project_invalid", "project manifest could not be loaded"
        )
    definition = project.definition
    driver = definition.lifecycle_driver
    policy = definition.test_receipt_policy
    checks = [definition.SCHEMA == PROJECT_PROTOCOL_SCHEMA]
    driver_report: dict[str, object] | None = None
    suite_report: dict[str, object] | None = None
    derived: dict[str, object] = {
        "driver_configured": driver is not None,
        "driver_identity_matches": None,
        "runner_identity_matches": None,
    }
    if isinstance(driver, StandardProjectLifecycleDriver):
        resolution_error: dict[str, str] | None = None
        try:
            resolve_standard_project_lifecycle_driver(driver)
        except StandardLifecycleBindingError as exc:
            driver_matches = False
            resolution_error = {"code": exc.code, "message": exc.message}
        else:
            driver_matches = True
        derived.update(
            {
                "configured_driver_identity": driver.identity.uri,
                "computed_driver_identity": (
                    driver.identity.uri if driver_matches else None
                ),
                "driver_identity_matches": driver_matches,
                "driver_resolution_error": resolution_error,
            }
        )
        driver_report = {
            "schema": driver.SCHEMA,
            "binding": driver.binding,
            "framework_distribution_identity": (
                driver.framework_distribution_identity.uri
            ),
            "policy_identity": driver.policy_identity.uri,
        }
        checks.extend((driver.SCHEMA == LIFECYCLE_DRIVER_SCHEMA, driver_matches))
    elif driver is not None:
        actual_driver = lifecycle_driver_implementation_identity(project, driver)
        driver_matches = actual_driver == driver.implementation_identity
        derived.update(
            {
                "configured_driver_identity": driver.implementation_identity.uri,
                "computed_driver_identity": actual_driver.uri,
                "driver_identity_matches": driver_matches,
            }
        )
        driver_report = {
            "schema": driver.SCHEMA,
            "driver_id": driver.driver_id,
            "version": driver.version,
        }
        checks.extend((driver.SCHEMA == LIFECYCLE_DRIVER_SCHEMA, driver_matches))
    if policy is not None:
        suite_report = {
            "schema": policy.SCHEMA,
            "suite_id": policy.suite_id,
            "suite_version": policy.suite_version,
            "runner_identity": policy.runner_identity.uri,
        }
        checks.append(policy.SCHEMA == TEST_SUITE_POLICY_SCHEMA)
        runner_paths = tuple(
            project.root / path for path in SAMPLE_TEST_RUNNER_SOURCE_PATHS
        )
        if all(path.is_file() and not path.is_symlink() for path in runner_paths):
            computed = sample_test_runner_source_closure_identity(project.root).uri
            runner_matches = computed == policy.runner_identity.uri
            derived.update(
                {
                    "computed_runner_identity": computed,
                    "runner_identity_matches": runner_matches,
                }
            )
            checks.append(runner_matches)
    components = _component_contracts(project)
    checks.append(bool(components["ok"]))
    if definition.project_id == "literate-ai":
        checks.append(definition.version == DISTRIBUTION_VERSION)
        if driver is not None and not isinstance(
            driver, StandardProjectLifecycleDriver
        ):
            checks.append(driver.version == DISTRIBUTION_VERSION)
        if policy is not None:
            checks.append(policy.suite_version == DISTRIBUTION_VERSION)
    return (
        {
            "state": "loaded",
            "project_id": definition.project_id,
            "protocol_schema": definition.SCHEMA,
            "project_version": definition.version,
            "lifecycle_driver": driver_report,
            "test_suite": suite_report,
            "component_contracts": components,
            "derived_identities": derived,
            "ok": all(checks),
        },
        project.root,
    )


def check_versions(
    *,
    project_path: Path | None = Path("."),
    require_project: bool = False,
    require_release_tag: bool = False,
) -> dict[str, Any]:
    """Return complete version agreement without mutating project or Git state."""

    project, project_root = _project_state(project_path)
    if require_project and project["state"] != "loaded":
        project["ok"] = False
    metadata_version = _distribution_metadata_version()
    editable_install = _distribution_is_editable()
    metadata_matches = metadata_version == DISTRIBUTION_VERSION
    stale_editable_metadata = (
        editable_install and metadata_version is not None and not metadata_matches
    )
    distribution_ok = metadata_matches or stale_editable_metadata
    distribution_diagnostic = (
        {
            "code": "distribution.editable_metadata_stale",
            "message": (
                "editable installer metadata is stale; reinstall with "
                "`pip install -e .` to synchronize it"
            ),
        }
        if stale_editable_metadata
        else None
    )
    schema_reports: dict[str, object] = {}
    schema_ok = True
    for version in SUPPORTED_SCHEMA_CATALOGS:
        try:
            root = _schema_root(version, project_root)
            report = verify_schema_catalog(version, root)
            if version == "v1":
                report = {**report, "published": verify_published_schemas(root)}
            expected_release = SCHEMA_CATALOG_RELEASES[version]
            if version == "v2" and report["release"] != expected_release:
                schema_ok = False
            schema_reports[version] = report
        except (FileNotFoundError, PublishedSchemaError, SchemaCatalogError) as exc:
            schema_ok = False
            schema_reports[version] = {"error": str(exc)}
    tag = _git_state(project_root, require_release_tag=require_release_tag)
    ok = distribution_ok and schema_ok and bool(project["ok"]) and bool(tag["ok"])
    return {
        "schema": VERSION_CHECK_SCHEMA,
        "ok": ok,
        "distribution": {
            "authority": "literate_ai.version:DISTRIBUTION_VERSION",
            "authority_version": DISTRIBUTION_VERSION,
            "package_version": DISTRIBUTION_VERSION,
            "wheel_metadata_version": metadata_version,
            "cli_version": DISTRIBUTION_VERSION,
            "install_kind": "editable" if editable_install else "non-editable",
            "metadata_matches_authority": metadata_matches,
            "diagnostic": distribution_diagnostic,
            "ok": distribution_ok,
        },
        "tag": tag,
        "schema_catalogs": schema_reports,
        "project": project,
    }


__all__ = ["VERSION_CHECK_SCHEMA", "VersionCheckError", "check_versions"]
