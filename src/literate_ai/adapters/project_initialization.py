"""Filesystem adapter for canonical project initialization and review recording."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from importlib import metadata
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from literate_ai.adapters.component_lock_application import update_component_lock
from literate_ai.adapters.component_lock_planning import ComponentLockPlanningError
from literate_ai.adapters.component_locks import ComponentLockStoreError
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStoreError,
)
from literate_ai.adapters.harness_inventory import (
    HARNESS_COMMAND_TIMEOUT_SECONDS,
    HARNESS_DIAGNOSTIC_CHARS,
    has_bazel_markers,
    inspect_harness,
    repo_driver_locations,
    source_language_markers,
    validate_harness_command_timeout,
    validate_harness_diagnostic_limit,
)
from literate_ai.adapters.harness_workspace import (
    HarnessWorkspaceError,
    HarnessWorkspaceLink,
    harness_workspace_link_evidence,
    materialize_harness_workspace_links,
    validate_harness_workspace_links,
)
from literate_ai.adapters.models.coding_cli import CODING_CLIS
from literate_ai.adapters.monorepo_adoption import (
    build_root_candidates,
    plan_monorepo_adoption,
)
from literate_ai.adapters.project_validation import (
    FilesystemProjectValidationAdapter,
    ProjectValidationError,
)
from literate_ai.adapters.repository_catalogs import (
    InheritedCatalogPlan,
    RepositoryCatalogError,
    materialize_inherited_catalogs,
    plan_inherited_catalogs,
)
from literate_ai.adapters.repository_lineage import (
    REPOSITORY_LINEAGE_FILE,
    REPOSITORY_PARENT_FILE,
    FilesystemRepositoryLineageStore,
    GitRepositoryLineageError,
    GitRepositorySnapshotProvider,
    RepositoryLineageStoreError,
    repository_parent_reference,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    StandardLifecycleBindingError,
    observe_installed_framework_distribution,
    observe_installed_framework_origin,
)
from literate_ai.adapters.user_paths import UserPathError, resolve_user_paths
from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)
from literate_ai.application.component_lock_resolution import (
    ComponentLockResolutionError,
)
from literate_ai.application.flavor_selection import (
    FlavorSelectionError,
    parse_flavor_selector_body,
)
from literate_ai.application.project_authority import (
    AUTHORITY_REVIEW_MARKER,
    AUTHORITY_REVIEW_PLACEHOLDER,
)
from literate_ai.application.repository_lineage import (
    RepositoryLineageResolutionError,
    resolve_repository_lineage,
)
from literate_ai.application.repository_parent_follow import highest_release_tag
from literate_ai.contracts import (
    DEFAULT_PROJECT_TYPE,
    PROJECT_TEMPLATE_PROTOCOL,
    PROJECT_TYPES,
    PROJECT_TYPES_WITH_STARTER,
    ContentIdentity,
    HashAlgorithm,
    ProjectDefinition,
    ProjectInitializationBaseline,
    ProjectInitializationBaselineFile,
    ProjectInitializationOrigin,
    ProjectSourceIntelligencePolicy,
    ProjectTestReceiptPolicy,
    RepositoryLineage,
    RepositoryParentReference,
    RepositoryParentSelection,
    RepositoryPolicy,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    StandardProjectLifecycleDriver,
    canonical_json_bytes,
    load_current_standard_lifecycle_policy,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    ProjectConfigurationStore,
    ProjectError,
    discover_project,
    documentation_files,
)
from literate_ai.version import DISTRIBUTION_VERSION

PROJECT_INITIALIZATION_SCHEMA = "literate-ai/project-initialization@6"
PROJECT_INITIALIZATION_PREREQUISITES_SCHEMA = (
    "literate-ai/project-initialization-prerequisites@1"
)


def _initialization_prerequisites() -> dict[str, object]:
    """Report mandatory and next-stage tools without invoking either toolchain."""

    selected_cli = os.environ.get("CODING_CLI", "").strip()
    candidates = (selected_cli,) if selected_cli else CODING_CLIS
    coding_cli = next(
        (
            {"name": name, "command": command, "state": "available-path"}
            for name in candidates
            if name in CODING_CLIS
            and (command := shutil.which(name, path=os.environ.get("PATH"))) is not None
        ),
        None,
    )
    if coding_cli is None:
        coding_cli = {
            "name": selected_cli or None,
            "command": None,
            "state": "unavailable-path",
        }
    return {
        "schema": PROJECT_INITIALIZATION_PREREQUISITES_SCHEMA,
        "python": {
            "required": True,
            "state": "available-runtime",
            "command": sys.executable,
            "version": platform.python_version(),
        },
        "coding_cli": {
            "required": False,
            "required_for": "source-generation",
            **coding_cli,
        },
    }


DEFAULT_AUTHORITY_REVIEW_DOCUMENT = "docs/architecture/design-traceability.md"
INITIALIZATION_ORIGIN_FILE = ".literate/initialization-origin.json"
INITIALIZATION_BASELINE_FILE = ".literate/initialization-baseline.json"
_MAXIMUM_INITIALIZED_FILE_BYTES = 16 * 1024 * 1024
_PRESERVED_INITIALIZATION_ENTRIES = frozenset({".git", "README.md"})

# Accepted Flavor selector aliases → human-readable axis label, derived from the
# shipped Flavor catalog itself (never hand-maintained): each flavor contributes its
# directory name, canonical name, and target value as aliases. Durable project state
# uses the full coordinates in FLAVOR_SELECTOR_CANONICAL_NAMES.


def _derived_flavor_tables() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Derive alias/axis, directory/coordinate, and selector-alias tables.

    The shipped ``project_template/flavors`` tree is the single source of truth for
    which Flavors ``litai init`` can select. Adding a flavor directory with a valid
    ``flavor.md`` is sufficient; no Python registry edits are required. Directory
    names, authored names, and ``target`` values all resolve to the canonical
    coordinate so axis-qualified directories like ``lang-python`` and
    ``lang-typescript`` still accept ``+python`` and ``+typescript``.
    """

    from literate_ai.adapters.flavor_markdown import parse_flavor_markdown

    alias_axes: dict[str, str] = {}
    canonical_by_directory: dict[str, str] = {}
    canonical_by_alias: dict[str, str] = {}
    template_flavors = files("literate_ai.project_template").joinpath("flavors")
    for entry in sorted(template_flavors.iterdir(), key=lambda item: item.name):
        manifest = entry / "flavor.md"
        if not entry.is_dir() or not manifest.is_file():
            continue
        authoring = parse_flavor_markdown(
            manifest.read_bytes(),
            source=f"flavors/{entry.name}/flavor.md",
        )
        coordinate = f"flavor://{authoring.namespace}/{authoring.name}"
        canonical_by_directory[entry.name] = coordinate
        axis = (
            authoring.primary_axis.value
            if hasattr(authoring.primary_axis, "value")
            else str(authoring.primary_axis)
        )
        for alias in (entry.name, authoring.name, authoring.target):
            if not alias:
                continue
            alias_axes[alias] = axis
            canonical_by_alias[alias] = coordinate
    return alias_axes, canonical_by_directory, canonical_by_alias


_FLAVOR_ALIAS_AXES: dict[str, str]
_FLAVOR_CANONICAL_BY_DIRECTORY: dict[str, str]
_FLAVOR_SELECTOR_CANONICAL_BY_NAME: dict[str, str]
(
    _FLAVOR_ALIAS_AXES,
    _FLAVOR_CANONICAL_BY_DIRECTORY,
    _FLAVOR_SELECTOR_CANONICAL_BY_NAME,
) = _derived_flavor_tables()

FLAVOR_SELECTOR_CANONICAL_NAMES: dict[str, str] = {
    **_FLAVOR_SELECTOR_CANONICAL_BY_NAME,
    **{
        coordinate: coordinate for coordinate in _FLAVOR_CANONICAL_BY_DIRECTORY.values()
    },
}

# Extra selector aliases that are not the Flavor's directory, canonical name, or
# target value. Durable project state is always rewritten to the canonical name.
DEPRECATED_FLAVOR_ALIASES: dict[str, str] = {
    "apt": "flavor://literate-ai/package-apt",
    "brew": "flavor://literate-ai/package-brew",
    "chocolatey": "flavor://literate-ai/package-chocolatey",
    "conan": "flavor://literate-ai/package-conan",
    "cuda": "flavor://literate-ai/accel-nvidia-cuda",
    "pip": "flavor://literate-ai/package-pip",
    "winget": "flavor://literate-ai/package-winget",
    "accelerator-nvidia-cuda": "flavor://literate-ai/accel-nvidia-cuda",
    "google-workspace": "flavor://literate-ai/doc-google-workspace",
    "microsoft-365": "flavor://literate-ai/doc-microsoft-365",
    "flavor://literate-ai/accelerator-nvidia-cuda": "flavor://literate-ai/accel-nvidia-cuda",
    "flavor://literate-ai/google-workspace": "flavor://literate-ai/doc-google-workspace",
    "flavor://literate-ai/microsoft-365": "flavor://literate-ai/doc-microsoft-365",
}

# ADR 0010 dotted spellings stay identity aliases for catalog collision and
# migrate-flavor-names, but they are no longer accepted as live selectors.
_CLOSED_DOTTED_FLAVOR_ALIASES: dict[str, str] = {
    "package.apt": "flavor://literate-ai/package-apt",
    "package.brew": "flavor://literate-ai/package-brew",
    "package.chocolatey": "flavor://literate-ai/package-chocolatey",
    "package.conan": "flavor://literate-ai/package-conan",
    "package.pip": "flavor://literate-ai/package-pip",
    "package.winget": "flavor://literate-ai/package-winget",
    "flavor://literate-ai/package.apt": "flavor://literate-ai/package-apt",
    "flavor://literate-ai/package.brew": "flavor://literate-ai/package-brew",
    "flavor://literate-ai/package.chocolatey": "flavor://literate-ai/package-chocolatey",
    "flavor://literate-ai/package.conan": "flavor://literate-ai/package-conan",
    "flavor://literate-ai/package.pip": "flavor://literate-ai/package-pip",
    "flavor://literate-ai/package.winget": "flavor://literate-ai/package-winget",
}
FLAVOR_SELECTOR_CANONICAL_NAMES.update(DEPRECATED_FLAVOR_ALIASES)
FLAVOR_SELECTOR_CANONICAL_NAMES["lang-javascript-react"] = (
    "flavor://literate-ai/ui-react"
)
KNOWN_FLAVOR_SELECTORS: dict[str, str] = {
    **_FLAVOR_ALIAS_AXES,
    **{
        FLAVOR_SELECTOR_CANONICAL_NAMES[alias]: axis
        for alias, axis in _FLAVOR_ALIAS_AXES.items()
    },
}
for _deprecated, _canonical in DEPRECATED_FLAVOR_ALIASES.items():
    if _canonical in KNOWN_FLAVOR_SELECTORS:
        KNOWN_FLAVOR_SELECTORS[_deprecated] = KNOWN_FLAVOR_SELECTORS[_canonical]
_UI_REACT_COORDINATE = "flavor://literate-ai/ui-react"
if _UI_REACT_COORDINATE in KNOWN_FLAVOR_SELECTORS:
    KNOWN_FLAVOR_SELECTORS["lang-javascript-react"] = KNOWN_FLAVOR_SELECTORS[
        _UI_REACT_COORDINATE
    ]
_FLAVOR_DIRECTORY_BY_CANONICAL = {
    coordinate: directory
    for directory, coordinate in _FLAVOR_CANONICAL_BY_DIRECTORY.items()
}


def canonical_flavor_coordinate(name_or_uri: str) -> str:
    """Map a Flavor name, directory, or coordinate URI to its canonical URI.

    Bare ADR 0010 aliases (`pip`, `google-workspace`, …) and shipped catalog
    target values (`python` → `lang-python`) resolve to the durable
    `flavor://…` coordinate. Closed dotted spellings remain identity aliases so
    leftover catalog entries still collide instead of forking. Unknown names keep
    a `flavor://literate-ai/` prefix only when they are not already a coordinate
    URI.
    """

    if not isinstance(name_or_uri, str) or not name_or_uri:
        raise ValueError("Flavor coordinate requires a name")
    mapped = FLAVOR_SELECTOR_CANONICAL_NAMES.get(
        name_or_uri, _CLOSED_DOTTED_FLAVOR_ALIASES.get(name_or_uri, name_or_uri)
    )
    if not mapped.startswith("flavor://"):
        mapped = FLAVOR_SELECTOR_CANONICAL_NAMES.get(
            mapped,
            _CLOSED_DOTTED_FLAVOR_ALIASES.get(mapped, f"flavor://literate-ai/{mapped}"),
        )
    return FLAVOR_SELECTOR_CANONICAL_NAMES.get(
        mapped, _CLOSED_DOTTED_FLAVOR_ALIASES.get(mapped, mapped)
    )


def canonical_flavor_catalog_collisions(
    entries: Sequence[tuple[str, str, str]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return canonical coordinates that appear at more than one catalog location.

    Each entry is ``(location, flavor_id, coordinate_uri)``. Location should be
    the Flavor directory path. Directory names, authored names, and coordinates
    all participate so ``flavors/pip`` and ``flavors/package-pip`` collide even
    when their ``flavor.md`` names still differ.
    """

    grouped: dict[str, list[str]] = {}
    for location, flavor_id, coordinate_uri in entries:
        identities = {
            canonical_flavor_coordinate(coordinate_uri),
            canonical_flavor_coordinate(flavor_id),
            canonical_flavor_coordinate(Path(location).name),
        }
        for identity in identities:
            seen = grouped.setdefault(identity, [])
            if location not in seen:
                seen.append(location)
    return tuple(
        (identity, tuple(sorted(locations)))
        for identity, locations in sorted(grouped.items())
        if len(locations) > 1
    )


def canonical_flavor_selector(selector: str) -> str:
    """Expand one signed compatibility selector to its canonical coordinate."""

    if not isinstance(selector, str) or len(selector) < 2 or selector[0] not in "+-":
        raise ValueError("Flavor selector must begin with + or -")
    try:
        slot_id, alias = parse_flavor_selector_body(selector[1:])
    except FlavorSelectionError as exc:
        raise ValueError(exc.message) from exc
    if alias in _CLOSED_DOTTED_FLAVOR_ALIASES:
        raise ValueError(
            "dotted Flavor selectors are closed; use the axis-prefixed dash name "
            "or the bare target (package-pip or pip, not package.pip)"
        )
    canonical = FLAVOR_SELECTOR_CANONICAL_NAMES.get(alias, alias)
    qualifier = f"{slot_id}:" if slot_id is not None else ""
    return f"{selector[0]}{qualifier}{canonical}"


def flavor_selector_directory(selector: str) -> str:
    """Resolve one signed built-in selector to its packaged catalog directory."""

    canonical = canonical_flavor_selector(selector)
    _slot_id, alias = parse_flavor_selector_body(canonical[1:])
    return _FLAVOR_DIRECTORY_BY_CANONICAL.get(alias, alias)


# Flavors whose presence triggers document-pair component+skill installation.
_DOCUMENT_PAIR_FLAVORS: frozenset[str] = frozenset(
    {"doc-google-workspace", "doc-microsoft-365"}
)


_LANGUAGE_FLAVOR_BY_MARKER = {
    "cpp": "flavor://literate-ai/lang-cpp",
    "python": "flavor://literate-ai/lang-python",
    "rust": "flavor://literate-ai/lang-rust",
    "javascript": "flavor://literate-ai/lang-javascript",
}


def detected_language_flavors(
    path: Path, *, languages: set[str] | None = None
) -> list[str]:
    """Return every detected language Flavor, C++ first when both C++ and Python exist.

    Project defaults may select only one language-ecosystem Flavor. Convert planning
    still lists the full union so operators see mixed trees.
    """

    if languages is None:
        languages = source_language_markers(path)
    flavors: list[str] = []
    if "cpp" in languages or (path / "CMakeLists.txt").exists():
        flavors.append(_LANGUAGE_FLAVOR_BY_MARKER["cpp"])
    if "python" in languages:
        flavors.append(_LANGUAGE_FLAVOR_BY_MARKER["python"])
    if flavors:
        return flavors
    if (path / "Cargo.toml").exists():
        return [_LANGUAGE_FLAVOR_BY_MARKER["rust"]]
    if (path / "package.json").exists():
        return [_LANGUAGE_FLAVOR_BY_MARKER["javascript"]]
    return []


def detect_repo_flavors(
    path: Path, *, language_flavors: list[str] | None = None
) -> list[str]:
    """Scan an existing repository and return inferred bare Flavor selector names."""

    selectors: list[str] = []
    covered_axes: set[str] = set()

    if language_flavors is None:
        language_flavors = detected_language_flavors(path)
    if language_flavors:
        selectors.append(language_flavors[0])
        covered_axes.add("implementation.language-ecosystem")

    # Build system detection. repo.sh owns the operator-facing command when present.
    # CMake owns the build directory whenever its manifest is present (CMake often
    # also generates Makefiles, which must not shadow it).
    if repo_driver_locations(path):
        selectors.append("flavor://literate-ai/build-repo-man")
        covered_axes.add("build.system")
    elif (path / "CMakeLists.txt").exists():
        selectors.append("flavor://literate-ai/build-cmake")
        covered_axes.add("build.system")
    elif has_bazel_markers(path):
        selectors.append("flavor://literate-ai/build-bazel")
        covered_axes.add("build.system")
    elif (path / "Cargo.toml").exists():
        selectors.append("flavor://literate-ai/build-cargo")
        covered_axes.add("build.system")
    else:
        make_files = ("Makefile", "GNUMakefile", "makefile", "GNUmakefile")
        if any((path / m).exists() for m in make_files):
            selectors.append("flavor://literate-ai/build-make")
            covered_axes.add("build.system")

    # Apply language/build defaults for uncovered axes
    for axis, default in _FLAVOR_AXIS_DEFAULTS.items():
        if axis not in covered_axes:
            selectors.append(default)

    return selectors


CONVERT_PLAN_SCHEMA = "literate-ai/convert-readiness@1"


def _git_probe(root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _conversion_git_submodules(root: Path) -> tuple[dict[str, str], ...]:
    """Return index Gitlinks without interpreting their worktrees as directories."""

    if _git_probe(root, "rev-parse", "--is-inside-work-tree") != "true":
        git_boundary = root / ".git"
        if git_boundary.exists() or git_boundary.is_symlink():
            raise ProjectInitializationError(
                "project.convert_submodule_inspection_failed",
                "conversion could not inspect the repository Git boundary safely",
            )
        return ()
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), "ls-files", "--stage", "-z"),
            check=False,
            capture_output=True,
            timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectInitializationError(
            "project.convert_submodule_inspection_failed",
            "conversion could not inspect Gitlink entries safely",
        ) from exc
    if completed.returncode != 0:
        raise ProjectInitializationError(
            "project.convert_submodule_inspection_failed",
            "conversion could not inspect Gitlink entries safely",
        )
    result: list[dict[str, str]] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        metadata, separator, raw_path = raw.partition(b"\t")
        if not separator or metadata.split(b" ", 1)[0] != b"160000":
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectInitializationError(
                "project.convert_submodule_inspection_failed",
                "Git reported a non-UTF-8 submodule path",
            ) from exc
        relative = PurePosixPath(path)
        if (
            not path
            or "\\" in path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != path
        ):
            raise ProjectInitializationError(
                "project.convert_submodule_inspection_failed",
                "Git reported an unsafe submodule path",
            )
        worktree = root.joinpath(*relative.parts)
        state = (
            "initialized"
            if worktree.is_dir() and (worktree / ".git").exists()
            else "uninitialized"
        )
        result.append({"path": path, "state": state})
    return tuple(sorted(result, key=lambda item: item["path"]))


def _conversion_repository_policy(
    root: Path, explicit_default_branch: str | None
) -> tuple[RepositoryPolicy, dict[str, str]]:
    if explicit_default_branch is not None:
        try:
            policy = RepositoryPolicy(default_branch=explicit_default_branch)
        except (TypeError, ValueError) as exc:
            raise ProjectInitializationError(
                "project.convert_default_branch_invalid",
                "--default-branch must name a safe Git branch",
            ) from exc
        return policy, {"branch": policy.default_branch, "source": "cli"}

    if _git_probe(root, "rev-parse", "--is-inside-work-tree") != "true":
        policy = RepositoryPolicy()
        return policy, {
            "branch": policy.default_branch,
            "source": "framework-default-no-git",
        }

    remote_names = set((_git_probe(root, "remote") or "").splitlines())
    upstream = _git_probe(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    if upstream is not None:
        upstream_remote = next(
            (
                name
                for name in sorted(remote_names, key=lambda item: (-len(item), item))
                if upstream.startswith(f"{name}/")
            ),
            None,
        )
        if upstream_remote is not None:
            branch = upstream.removeprefix(f"{upstream_remote}/")
            policy = RepositoryPolicy(default_branch=branch, remote=upstream_remote)
            return policy, {"branch": branch, "source": "current-upstream"}

    remote = RepositoryPolicy().remote
    remote_head = _git_probe(
        root, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"
    )
    prefix = f"refs/remotes/{remote}/"
    if remote_head is not None and remote_head.startswith(prefix):
        branch = remote_head.removeprefix(prefix)
        policy = RepositoryPolicy(default_branch=branch, remote=remote)
        return policy, {"branch": branch, "source": "remote-head"}

    current = _git_probe(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if remote in remote_names:
        tracked = (
            None
            if current is None
            else _git_probe(
                root,
                "show-ref",
                "--verify",
                f"refs/remotes/{remote}/{current}",
            )
        )
        if current is not None and tracked is not None:
            policy = RepositoryPolicy(default_branch=current, remote=remote)
            return policy, {
                "branch": current,
                "source": "current-remote-tracking-branch",
            }
        raise ProjectInitializationError(
            "project.convert_default_branch_unresolved",
            f"could not derive {remote!r}'s default branch from local Git refs; "
            "fetch the remote HEAD or pass --default-branch",
        )
    if current is not None:
        policy = RepositoryPolicy(default_branch=current)
        return policy, {"branch": current, "source": "local-current-no-remote"}
    raise ProjectInitializationError(
        "project.convert_default_branch_unresolved",
        "detached Git conversion has no configured remote default; pass "
        "--default-branch",
    )


def _convert_catalog_gaps(proposed: list[str]) -> list[dict[str, str]]:
    """Return proposed Flavors that init cannot stamp from the shipped template."""

    gaps: list[dict[str, str]] = []
    for selector in proposed:
        canonical = FLAVOR_SELECTOR_CANONICAL_NAMES.get(selector, selector)
        if (
            canonical not in KNOWN_FLAVOR_SELECTORS
            and selector not in KNOWN_FLAVOR_SELECTORS
        ):
            gaps.append({"selector": selector, "reason": "unknown-flavor"})
            continue
        directory = _FLAVOR_DIRECTORY_BY_CANONICAL.get(canonical)
        if directory is None:
            gaps.append({"selector": selector, "reason": "unknown-flavor-directory"})
            continue
        flavor_path = f"flavors/{directory}/flavor.md"
        if flavor_path not in _TEMPLATE_FILES:
            gaps.append({"selector": selector, "reason": "not-in-init-template"})
    return gaps


def plan_convert(
    root: Path,
    *,
    default_branch: str | None = None,
    root_plan: Path | None = None,
) -> dict[str, object]:
    """Inspect a live tree and report convert readiness without writing files."""

    if not root.is_dir():
        raise ProjectInitializationError(
            "project.init_target_invalid",
            "convert plan target must be an existing project directory",
        )
    inventory = inspect_harness(root)
    languages = source_language_markers(root)
    language_flavors = detected_language_flavors(root, languages=languages)
    proposed = detect_repo_flavors(root, language_flavors=language_flavors)
    proposed.extend(flavor for flavor in language_flavors if flavor not in proposed)
    catalog_gaps = _convert_catalog_gaps(proposed)
    submodules = _conversion_git_submodules(root)
    refinement = (
        None
        if root_plan is None
        else plan_monorepo_adoption(
            root,
            root_plan,
            inventory,
            submodules=list(submodules),
        )
    )
    _policy, repository_default_branch = _conversion_repository_policy(
        root, default_branch
    )
    findings = [
        *inventory["findings"],
        *(
            {
                "detector_id": "repository.git-submodule",
                "path": item["path"],
                "detail": f"Git submodule ({item['state']})",
            }
            for item in submodules
        ),
    ]
    stages = inventory["stages"]
    if catalog_gaps:
        readiness = "blocked-missing-catalog"
    elif not stages:
        readiness = "blocked-no-driver"
    else:
        readiness = "ready"
    quarantine_entries = sorted(
        item.name for item in root.iterdir() if item.name != ".git"
    )
    return {
        "schema": CONVERT_PLAN_SCHEMA,
        "readiness": readiness,
        "findings": findings,
        "proposed_flavors": proposed,
        "proposed_wrapper_stages": stages,
        "build_root_candidates": build_root_candidates(inventory),
        "root_refinement": refinement,
        "catalog_gaps": catalog_gaps,
        "ci": inventory["ci"],
        "languages": sorted(languages),
        "repository_default_branch": repository_default_branch,
        "submodules": list(submodules),
        # Gitlinks are retained as findings and relocated with ``git mv`` during
        # quarantine, so their presence is not a conversion blocker.
        "gitlink_blockers": [],
        "quarantine": {
            "root": CONVERT_LEGACY_DIRECTORY,
            "entries": quarantine_entries,
            "destination": "generated-transaction-directory-on-apply",
        },
        # The complete typed stage descriptions remain in
        # ``proposed_wrapper_stages``.  This field is the stable operator-facing
        # runner inventory, so keep only the deterministic stage identifiers.
        "retained_runners": sorted(
            str(stage["id"])
            for stage in stages
            if isinstance(stage, dict) and isinstance(stage.get("id"), str)
        ),
        "landing_stage": "wrapped",
        "writes": False,
    }


CONVERT_QUARANTINE_SCHEMA = "literate-ai/project-convert-quarantine@1"
CONVERT_LEGACY_DIRECTORY = "_legacy"


def _git_entry_is_fully_tracked(target: Path, entry: Path) -> bool:
    """Return True when every regular file beneath *entry* is git-tracked."""

    relative = entry.relative_to(target).as_posix()
    listed = subprocess.run(
        ("git", "-C", str(target), "ls-files", "-z", "--", relative),
        check=True,
        capture_output=True,
    )
    tracked = {item for item in listed.stdout.split(b"\0") if item}
    if not tracked:
        return False
    if entry.is_file():
        return True
    actual = {
        item.relative_to(target).as_posix().encode("utf-8")
        for item in entry.rglob("*")
        if item.is_file() and not item.is_symlink()
    }
    return actual <= tracked


def _git_entry_contains_gitlink(target: Path, entry: Path) -> bool:
    """Return whether a tracked Gitlink is nested beneath *entry*.

    Git can relocate a submodule and repair its administrative metadata with
    ``git mv``. A filesystem move cannot do that: the nested worktree's
    ``.git`` file would retain its old relative location. Treat the enclosing
    legacy entry as a Git move even when its ordinary files are not all tracked
    by the superproject.
    """

    relative = entry.relative_to(target).as_posix()
    completed = subprocess.run(
        ("git", "-C", str(target), "ls-files", "--stage", "-z", "--", relative),
        check=True,
        capture_output=True,
    )
    return any(
        record.partition(b"\t")[0].split(b" ", 1)[0] == b"160000"
        for record in completed.stdout.split(b"\0")
        if record
    )


def _quarantine_existing_tree(target: Path) -> dict[str, object] | None:
    """Move every pre-existing entry at *target* except ``.git`` into one aside
    directory, preserving the legacy hierarchy intact beneath it.

    Git-tracked entries are moved with ``git mv`` (when the target resolves inside a
    Git work tree); every other entry is moved with a plain filesystem move. Returns a
    manifest describing the quarantine, or None when the target is empty.
    """

    entries = sorted(
        (item for item in target.iterdir() if item.name != ".git"),
        key=lambda item: item.name,
    )
    if not entries:
        return None
    digest_input = "\n".join(item.name for item in entries)
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:8]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    aside = target / CONVERT_LEGACY_DIRECTORY / f"{stamp}-{digest}"
    if aside.exists():
        raise ProjectInitializationError(
            "project.convert_quarantine_collision",
            f"quarantine destination {aside} already exists",
        )
    aside.mkdir(parents=True)
    use_git = (target / ".git").exists()
    moved: list[dict[str, str]] = []
    for entry in entries:
        if entry.is_symlink():
            resolved = entry.resolve()
            if target not in resolved.parents and resolved != target:
                raise ProjectInitializationError(
                    "project.convert_unsafe_entry",
                    f"refusing to quarantine symlink escaping the project: "
                    f"{entry.name}",
                )
        destination = aside / entry.name
        vcs = "fs"
        if (
            use_git
            and not entry.is_symlink()
            and (
                _git_entry_is_fully_tracked(target, entry)
                or _git_entry_contains_gitlink(target, entry)
            )
        ):
            completed = subprocess.run(
                (
                    "git",
                    "-C",
                    str(target),
                    "mv",
                    entry.name,
                    str(destination.relative_to(target)),
                ),
                capture_output=True,
            )
            if completed.returncode == 0:
                vcs = "git"
        if not destination.exists():
            shutil.move(str(entry), str(destination))
        moved.append(
            {
                "from": entry.name,
                "to": destination.relative_to(target).as_posix(),
                "vcs": vcs,
            }
        )
    return {
        "schema": CONVERT_QUARANTINE_SCHEMA,
        "directory": aside.relative_to(target).as_posix(),
        "moved": moved,
    }


def _rollback_converted_tree(target: Path, quarantine: dict[str, object]) -> None:
    """Remove the scaffold and restore one quarantined tree to its original paths."""

    directory = quarantine.get("directory")
    moved = quarantine.get("moved")
    if not isinstance(directory, str) or not isinstance(moved, list):
        raise ProjectInitializationError(
            "project.convert_rollback_manifest_invalid",
            "conversion rollback lacks its exact quarantine manifest",
        )
    aside = target / directory
    if not aside.is_dir() or aside.is_symlink():
        raise ProjectInitializationError(
            "project.convert_rollback_source_missing",
            "conversion rollback quarantine is unavailable",
        )
    # Nothing except repository metadata and the quarantine survives rollback.
    legacy_root_name = Path(directory).parts[0]
    for entry in sorted(target.iterdir(), key=lambda item: item.name):
        if entry.name in {".git", legacy_root_name}:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for item in sorted(moved, key=lambda value: str(value.get("from", ""))):
        if not isinstance(item, dict):
            raise ProjectInitializationError(
                "project.convert_rollback_manifest_invalid",
                "conversion rollback contains an invalid move record",
            )
        original = item.get("from")
        quarantined = item.get("to")
        vcs = item.get("vcs")
        if not isinstance(original, str) or not isinstance(quarantined, str):
            raise ProjectInitializationError(
                "project.convert_rollback_manifest_invalid",
                "conversion rollback contains an incomplete move record",
            )
        source = target / quarantined
        destination = target / original
        if not source.exists() or destination.exists():
            raise ProjectInitializationError(
                "project.convert_rollback_conflict",
                f"conversion rollback cannot restore {original!r}",
            )
        restored_with_git = False
        if vcs == "git" and (target / ".git").exists():
            completed = subprocess.run(
                ("git", "-C", str(target), "mv", quarantined, original),
                capture_output=True,
            )
            restored_with_git = completed.returncode == 0
        if not restored_with_git:
            shutil.move(str(source), str(destination))
    legacy_root = target / legacy_root_name
    if legacy_root.exists():
        shutil.rmtree(legacy_root)


def _restore_lift_shift_quarantine(
    target: Path,
    quarantine: dict[str, object],
    lift_shift: dict[str, object],
) -> None:
    """Recreate quarantine custody so the existing conversion rollback can run."""

    directory = quarantine.get("directory")
    moves = lift_shift.get("moves")
    if not isinstance(directory, str) or not isinstance(moves, list):
        raise ProjectInitializationError(
            "project.convert_rollback_manifest_invalid",
            "lift-shift rollback lacks its exact move manifest",
        )
    target.joinpath(*Path(directory).parts).mkdir(parents=True)
    for item in reversed(moves):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("from"), str)
            or not isinstance(item.get("to"), str)
        ):
            raise ProjectInitializationError(
                "project.convert_rollback_manifest_invalid",
                "lift-shift rollback contains an invalid move record",
            )
        source = target.joinpath(*Path(str(item["to"])).parts)
        destination = target.joinpath(*Path(str(item["from"])).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists() or destination.exists():
            raise ProjectInitializationError(
                "project.convert_rollback_conflict",
                f"lift-shift rollback cannot restore {item['from']!r}",
            )
        shutil.move(str(source), str(destination))
    implementation = target / "components" / "legacy-project-wrapper" / "implementation"
    if implementation.exists():
        shutil.rmtree(implementation)


# Per-axis default selector applied when init caller declares no Flavor on that axis.
# "platform.os" is handled separately by host_platform_selector().
# Remaining axes (documentation.ecosystem, toolchain, accelerator, deployment,
# platform.architecture) have no default; callers must opt in explicitly.
_FLAVOR_AXIS_DEFAULTS: dict[str, str] = {
    "implementation.language-ecosystem": "flavor://literate-ai/lang-python",
    "build.system": "flavor://literate-ai/build-make",
    "packaging": "flavor://literate-ai/package-pip",
}


def host_platform_selector() -> str:
    """Return the +selector string for the current host OS."""
    if sys.platform == "darwin":
        return "+flavor://literate-ai/os-macos"
    if sys.platform.startswith("linux"):
        return "+flavor://literate-ai/os-linux"
    return "+flavor://literate-ai/os-windows"


def apply_init_flavor_defaults(raw: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Normalize selectors and fill uncovered starter axes with init defaults."""

    normalized = [
        "+"
        + FLAVOR_SELECTOR_CANONICAL_NAMES.get(
            flavor.removeprefix("+"), flavor.removeprefix("+")
        )
        for flavor in raw
    ]
    canonical = {selector.removeprefix("+") for selector in normalized}
    covered = {
        KNOWN_FLAVOR_SELECTORS[coordinate]
        for coordinate in canonical
        if coordinate in KNOWN_FLAVOR_SELECTORS
    }
    for axis, default in _FLAVOR_AXIS_DEFAULTS.items():
        if axis == "packaging" or axis in covered:
            continue
        normalized.append("+" + default)
    selected_now = {item.removeprefix("+") for item in normalized}
    language_count = sum(
        1
        for coordinate in selected_now
        if KNOWN_FLAVOR_SELECTORS.get(coordinate) == "implementation.language-ecosystem"
    )
    if "packaging" not in covered:
        if language_count >= 2:
            normalized.append("+flavor://literate-ai/package-conan")
        else:
            normalized.append("+" + _FLAVOR_AXIS_DEFAULTS["packaging"])
    if "platform.os" not in covered:
        normalized.append(host_platform_selector())
    selected = {item.removeprefix("+") for item in normalized}
    if "flavor://literate-ai/lang-swift" in selected and "toolchain" not in covered:
        platform = next(item for item in selected if "/os-" in item)
        realization = {
            "flavor://literate-ai/os-macos": (
                "flavor://literate-ai/toolchain-swift-apple"
            ),
            "flavor://literate-ai/os-linux": (
                "flavor://literate-ai/toolchain-swift-linux"
            ),
            "flavor://literate-ai/os-windows": (
                "flavor://literate-ai/toolchain-swift-windows"
            ),
        }[platform]
        normalized.append("+" + realization)
    return tuple(normalized)


_TEMPLATE_FILES = {
    ".gitignore": "project.gitignore",
    "CHANGELOG.md": "CHANGELOG.md",
    "SKILL.md": "SKILL.md",
    "AGENTS.md": "AGENTS.md",
    "CLAUDE.md": "CLAUDE.md",
    "agents/openai.yaml": "agents/openai.yaml",
    "docs/roadmap/active-work.md": "docs/roadmap/active-work.md",
    "docs/user/test-matrix.md": "docs/user/test-matrix.md",
    "literate.test.example.json": "literate.test.example.json",
    "literate.workers.example.json": "literate.workers.example.json",
    "literate.release.json": "literate.release.json",
    ".cursor/rules/literate-ai.mdc": "cursor/literate-ai.mdc",
    "flavors/os-base/flavor.md": "flavors/os-base/flavor.md",
    "flavors/os-base/toolchain.cdx.json": "flavors/os-base/toolchain.cdx.json",
    "flavors/os-base/openspec/spec.md": "flavors/os-base/openspec/spec.md",
    "flavors/build-bazel/flavor.md": "flavors/build-bazel/flavor.md",
    "flavors/build-bazel/host-toolchain.cdx.json": (
        "flavors/build-bazel/host-toolchain.cdx.json"
    ),
    "flavors/build-bazel/standard-command-profile.json": (
        "flavors/build-bazel/standard-command-profile.json"
    ),
    "flavors/build-bazel/openspec/spec.md": "flavors/build-bazel/openspec/spec.md",
    "flavors/build-cmake/flavor.md": "flavors/build-cmake/flavor.md",
    "flavors/build-cmake/host-toolchain.cdx.json": (
        "flavors/build-cmake/host-toolchain.cdx.json"
    ),
    "flavors/build-cmake/standard-command-profile.json": (
        "flavors/build-cmake/standard-command-profile.json"
    ),
    "flavors/build-cmake/openspec/spec.md": "flavors/build-cmake/openspec/spec.md",
    "flavors/build-repo-man/flavor.md": "flavors/build-repo-man/flavor.md",
    "flavors/build-repo-man/standard-command-profile.json": (
        "flavors/build-repo-man/standard-command-profile.json"
    ),
    "flavors/build-repo-man/openspec/spec.md": (
        "flavors/build-repo-man/openspec/spec.md"
    ),
    "flavors/accel-nvidia-cuda/flavor.md": "flavors/accel-nvidia-cuda/flavor.md",
    "flavors/accel-nvidia-cuda/standard-command-profile.json": (
        "flavors/accel-nvidia-cuda/standard-command-profile.json"
    ),
    "flavors/accel-nvidia-cuda/toolchain.json": (
        "flavors/accel-nvidia-cuda/toolchain.json"
    ),
    "flavors/accel-nvidia-cuda/openspec/spec.md": (
        "flavors/accel-nvidia-cuda/openspec/spec.md"
    ),
    "flavors/build-make/flavor.md": "flavors/build-make/flavor.md",
    "flavors/build-make/host-toolchain.cdx.json": (
        "flavors/build-make/host-toolchain.cdx.json"
    ),
    "flavors/build-make/standard-command-profile.json": (
        "flavors/build-make/standard-command-profile.json"
    ),
    "flavors/build-make/openspec/spec.md": "flavors/build-make/openspec/spec.md",
    "flavors/build-cargo/flavor.md": "flavors/build-cargo/flavor.md",
    "flavors/build-cargo/host-toolchain.cdx.json": (
        "flavors/build-cargo/host-toolchain.cdx.json"
    ),
    "flavors/build-cargo/standard-command-profile.json": (
        "flavors/build-cargo/standard-command-profile.json"
    ),
    "flavors/build-cargo/openspec/spec.md": "flavors/build-cargo/openspec/spec.md",
    "flavors/lang-python/flavor.md": "flavors/lang-python/flavor.md",
    "flavors/lang-python/host-toolchain.cdx.json": (
        "flavors/lang-python/host-toolchain.cdx.json"
    ),
    "flavors/lang-python/toolchain.json": "flavors/lang-python/toolchain.json",
    "flavors/lang-python/standard-command-profile.json": (
        "flavors/lang-python/standard-command-profile.json"
    ),
    "flavors/lang-python/openspec/spec.md": "flavors/lang-python/openspec/spec.md",
    "flavors/lang-javascript/flavor.md": "flavors/lang-javascript/flavor.md",
    "flavors/lang-javascript/host-toolchain.cdx.json": (
        "flavors/lang-javascript/host-toolchain.cdx.json"
    ),
    "flavors/lang-javascript/toolchain.json": "flavors/lang-javascript/toolchain.json",
    "flavors/lang-javascript/standard-command-profile.json": (
        "flavors/lang-javascript/standard-command-profile.json"
    ),
    "flavors/lang-javascript/openspec/spec.md": (
        "flavors/lang-javascript/openspec/spec.md"
    ),
    "flavors/lang-typescript/flavor.md": "flavors/lang-typescript/flavor.md",
    "flavors/lang-typescript/host-toolchain.cdx.json": (
        "flavors/lang-typescript/host-toolchain.cdx.json"
    ),
    "flavors/lang-typescript/toolchain.json": (
        "flavors/lang-typescript/toolchain.json"
    ),
    "flavors/lang-typescript/standard-command-profile.json": (
        "flavors/lang-typescript/standard-command-profile.json"
    ),
    "flavors/lang-typescript/openspec/spec.md": (
        "flavors/lang-typescript/openspec/spec.md"
    ),
    "flavors/lang-elixir/flavor.md": "flavors/lang-elixir/flavor.md",
    "flavors/lang-elixir/toolchain.json": "flavors/lang-elixir/toolchain.json",
    "flavors/lang-elixir/standard-command-profile.json": (
        "flavors/lang-elixir/standard-command-profile.json"
    ),
    "flavors/lang-elixir/openspec/spec.md": "flavors/lang-elixir/openspec/spec.md",
    "flavors/lang-zig/flavor.md": "flavors/lang-zig/flavor.md",
    "flavors/lang-zig/toolchain.json": "flavors/lang-zig/toolchain.json",
    "flavors/lang-zig/standard-command-profile.json": (
        "flavors/lang-zig/standard-command-profile.json"
    ),
    "flavors/lang-zig/openspec/spec.md": "flavors/lang-zig/openspec/spec.md",
    "flavors/toolchain-zig-cc/flavor.md": "flavors/toolchain-zig-cc/flavor.md",
    "flavors/toolchain-zig-cc/toolchain.json": (
        "flavors/toolchain-zig-cc/toolchain.json"
    ),
    "flavors/toolchain-zig-cc/openspec/spec.md": (
        "flavors/toolchain-zig-cc/openspec/spec.md"
    ),
    "flavors/deploy-docker/flavor.md": "flavors/deploy-docker/flavor.md",
    "flavors/deploy-docker/openspec/spec.md": (
        "flavors/deploy-docker/openspec/spec.md"
    ),
    "flavors/ui-react/flavor.md": "flavors/ui-react/flavor.md",
    "flavors/ui-react/openspec/spec.md": "flavors/ui-react/openspec/spec.md",
    "flavors/lang-rust/flavor.md": "flavors/lang-rust/flavor.md",
    "flavors/lang-rust/host-toolchain.cdx.json": (
        "flavors/lang-rust/host-toolchain.cdx.json"
    ),
    "flavors/lang-rust/standard-command-profile.json": (
        "flavors/lang-rust/standard-command-profile.json"
    ),
    "flavors/lang-rust/openspec/spec.md": "flavors/lang-rust/openspec/spec.md",
    "flavors/lang-swift/flavor.md": "flavors/lang-swift/flavor.md",
    "flavors/lang-swift/standard-command-profile.json": (
        "flavors/lang-swift/standard-command-profile.json"
    ),
    "flavors/lang-swift/openspec/spec.md": "flavors/lang-swift/openspec/spec.md",
    "flavors/toolchain-swift-apple/flavor.md": (
        "flavors/toolchain-swift-apple/flavor.md"
    ),
    "flavors/toolchain-swift-apple/toolchain.json": (
        "flavors/toolchain-swift-apple/toolchain.json"
    ),
    "flavors/toolchain-swift-apple/openspec/spec.md": (
        "flavors/toolchain-swift-apple/openspec/spec.md"
    ),
    "flavors/toolchain-swift-linux/flavor.md": (
        "flavors/toolchain-swift-linux/flavor.md"
    ),
    "flavors/toolchain-swift-linux/toolchain.json": (
        "flavors/toolchain-swift-linux/toolchain.json"
    ),
    "flavors/toolchain-swift-linux/openspec/spec.md": (
        "flavors/toolchain-swift-linux/openspec/spec.md"
    ),
    "flavors/toolchain-swift-windows/flavor.md": (
        "flavors/toolchain-swift-windows/flavor.md"
    ),
    "flavors/toolchain-swift-windows/toolchain.json": (
        "flavors/toolchain-swift-windows/toolchain.json"
    ),
    "flavors/toolchain-swift-windows/openspec/spec.md": (
        "flavors/toolchain-swift-windows/openspec/spec.md"
    ),
    "flavors/lang-cpp/flavor.md": "flavors/lang-cpp/flavor.md",
    "flavors/lang-cpp/host-toolchain.cdx.json": (
        "flavors/lang-cpp/host-toolchain.cdx.json"
    ),
    "flavors/lang-cpp/standard-command-profile.json": (
        "flavors/lang-cpp/standard-command-profile.json"
    ),
    "flavors/lang-cpp/openspec/spec.md": "flavors/lang-cpp/openspec/spec.md",
    "flavors/os-macos/flavor.md": "flavors/os-macos/flavor.md",
    "flavors/os-macos/standard-command-profile.json": (
        "flavors/os-macos/standard-command-profile.json"
    ),
    "flavors/os-macos/openspec/spec.md": "flavors/os-macos/openspec/spec.md",
    "flavors/os-linux/flavor.md": "flavors/os-linux/flavor.md",
    "flavors/os-linux/standard-command-profile.json": (
        "flavors/os-linux/standard-command-profile.json"
    ),
    "flavors/os-linux/openspec/spec.md": "flavors/os-linux/openspec/spec.md",
    "flavors/os-windows/flavor.md": "flavors/os-windows/flavor.md",
    "flavors/os-windows/host-toolchain.cdx.json": (
        "flavors/os-windows/host-toolchain.cdx.json"
    ),
    "flavors/os-windows/standard-command-profile.json": (
        "flavors/os-windows/standard-command-profile.json"
    ),
    "flavors/os-windows/openspec/spec.md": "flavors/os-windows/openspec/spec.md",
    "flavors/package-pip/flavor.md": "flavors/package-pip/flavor.md",
    "flavors/package-zip/flavor.md": "flavors/package-zip/flavor.md",
    "flavors/package-zip/openspec/spec.md": "flavors/package-zip/openspec/spec.md",
    "flavors/package-pip/openspec/spec.md": "flavors/package-pip/openspec/spec.md",
    "flavors/package-npm/flavor.md": "flavors/package-npm/flavor.md",
    "flavors/package-npm/toolchain.json": "flavors/package-npm/toolchain.json",
    "flavors/package-npm/host-toolchain.cdx.json": (
        "flavors/package-npm/host-toolchain.cdx.json"
    ),
    "flavors/package-npm/standard-command-profile.json": (
        "flavors/package-npm/standard-command-profile.json"
    ),
    "flavors/package-npm/openspec/spec.md": "flavors/package-npm/openspec/spec.md",
    "flavors/package-conan/flavor.md": "flavors/package-conan/flavor.md",
    "flavors/package-conan/openspec/spec.md": "flavors/package-conan/openspec/spec.md",
    "flavors/package-apt/flavor.md": "flavors/package-apt/flavor.md",
    "flavors/package-apt/openspec/spec.md": "flavors/package-apt/openspec/spec.md",
    "flavors/package-brew/flavor.md": "flavors/package-brew/flavor.md",
    "flavors/package-brew/openspec/spec.md": "flavors/package-brew/openspec/spec.md",
    "flavors/package-winget/flavor.md": "flavors/package-winget/flavor.md",
    "flavors/package-winget/openspec/spec.md": (
        "flavors/package-winget/openspec/spec.md"
    ),
    "flavors/package-chocolatey/flavor.md": "flavors/package-chocolatey/flavor.md",
    "flavors/package-chocolatey/openspec/spec.md": (
        "flavors/package-chocolatey/openspec/spec.md"
    ),
    "flavors/doc-google-workspace/flavor.md": "flavors/doc-google-workspace/flavor.md",
    "flavors/doc-google-workspace/openspec/spec.md": (
        "flavors/doc-google-workspace/openspec/spec.md"
    ),
    "flavors/doc-google-workspace/document-pair.md": (
        "flavors/doc-google-workspace/document-pair.md"
    ),
    "flavors/doc-microsoft-365/flavor.md": "flavors/doc-microsoft-365/flavor.md",
    "flavors/doc-microsoft-365/openspec/spec.md": (
        "flavors/doc-microsoft-365/openspec/spec.md"
    ),
    "flavors/doc-microsoft-365/document-pair.md": (
        "flavors/doc-microsoft-365/document-pair.md"
    ),
    "components/document-pair/component.md": "components/document-pair/component.md",
    "components/document-pair/interfaces/document-pair.md": (
        "components/document-pair/interfaces/document-pair.md"
    ),
    "components/document-pair/acceptance/document-pair.md": (
        "components/document-pair/acceptance/document-pair.md"
    ),
    "skills/specification-to-source/document-pair/SKILL.md": (
        "skills/specification-to-source/document-pair/SKILL.md"
    ),
    "skills/agent/package-artifacts/SKILL.md": (
        "skills/agent/package-artifacts/SKILL.md"
    ),
    "skills/agent/prompt-master/SKILL.md": "skills/agent/prompt-master/SKILL.md",
    "skills/agent/prompt-master/LICENSE.prompt-master": (
        "skills/agent/prompt-master/LICENSE.prompt-master"
    ),
    # portable-application pins these by exact identity; init must ship them or every
    # initialized project fails validation with a missing skill dependency.
    "skills/specification-to-source/repository-layout/SKILL.md": (
        "skills/specification-to-source/repository-layout/SKILL.md"
    ),
    "skills/specification-to-source/python-repository-layout/SKILL.md": (
        "skills/specification-to-source/python-repository-layout/SKILL.md"
    ),
    "skills/specification-to-source/bazel-build-system/SKILL.md": (
        "skills/specification-to-source/bazel-build-system/SKILL.md"
    ),
    "skills/specification-to-source/cmake-build-system/SKILL.md": (
        "skills/specification-to-source/cmake-build-system/SKILL.md"
    ),
    "skills/specification-to-source/repo-man-build-system/SKILL.md": (
        "skills/specification-to-source/repo-man-build-system/SKILL.md"
    ),
    "skills/specification-to-source/generate-nvidia-cuda-application/SKILL.md": (
        "skills/specification-to-source/generate-nvidia-cuda-application/SKILL.md"
    ),
    (
        "skills/specification-to-source/generate-nvidia-cuda-application/"
        "agents/openai.yaml"
    ): (
        "skills/specification-to-source/generate-nvidia-cuda-application/"
        "agents/openai.yaml"
    ),
    "skills/agent/select-nvidia-accelerated-stack/SKILL.md": (
        "skills/agent/select-nvidia-accelerated-stack/SKILL.md"
    ),
    "skills/specification-to-source/make-build-system/SKILL.md": (
        "skills/specification-to-source/make-build-system/SKILL.md"
    ),
    "skills/specification-to-source/cargo-build-system/SKILL.md": (
        "skills/specification-to-source/cargo-build-system/SKILL.md"
    ),
    "skills/specification-to-source/rust-ecosystem/SKILL.md": (
        "skills/specification-to-source/rust-ecosystem/SKILL.md"
    ),
    "skills/specification-to-source/javascript-ecosystem/SKILL.md": (
        "skills/specification-to-source/javascript-ecosystem/SKILL.md"
    ),
    "skills/specification-to-source/cpp-ecosystem/SKILL.md": (
        "skills/specification-to-source/cpp-ecosystem/SKILL.md"
    ),
    "skills/specification-to-source/portable-application/SKILL.md": (
        "skills/specification-to-source/portable-application/SKILL.md"
    ),
    "skills/specification-to-source/portable-application-implementation/SKILL.md": (
        "skills/specification-to-source/portable-application-implementation/SKILL.md"
    ),
    "skills/specification-to-source/portable-specification-planning/SKILL.md": (
        "skills/specification-to-source/portable-specification-planning/SKILL.md"
    ),
    "skills/specification-to-source/debug-spec-map/SKILL.md": (
        "skills/specification-to-source/debug-spec-map/SKILL.md"
    ),
    "skills/specification-to-source/go-portable-application/SKILL.md": (
        "skills/specification-to-source/go-portable-application/SKILL.md"
    ),
    "skills/specification-to-source/python-portable-application/SKILL.md": (
        "skills/specification-to-source/python-portable-application/SKILL.md"
    ),
    "skills/specification-to-source/javascript-portable-json-application/SKILL.md": (
        "skills/specification-to-source/javascript-portable-json-application/SKILL.md"
    ),
    "skills/specification-to-source/mcp-application/SKILL.md": (
        "skills/specification-to-source/mcp-application/SKILL.md"
    ),
    "skills/specification-to-source/mcp-application/webmcp/SKILL.md": (
        "skills/specification-to-source/mcp-application/webmcp/SKILL.md"
    ),
    "skills/specification-to-source/frontend-application/SKILL.md": (
        "skills/specification-to-source/frontend-application/SKILL.md"
    ),
    "skills/specification-to-source/frontend-application/react-application/SKILL.md": (
        "skills/specification-to-source/frontend-application/react-application/SKILL.md"
    ),
    (
        "skills/specification-to-source/frontend-application/"
        "react-dashboard-application/SKILL.md"
    ): (
        "skills/specification-to-source/frontend-application/"
        "react-dashboard-application/SKILL.md"
    ),
    "skills/specification-to-source/backend-application/SKILL.md": (
        "skills/specification-to-source/backend-application/SKILL.md"
    ),
    (
        "skills/specification-to-source/backend-application/"
        "python-service-application/SKILL.md"
    ): (
        "skills/specification-to-source/backend-application/"
        "python-service-application/SKILL.md"
    ),
    (
        "skills/specification-to-source/backend-application/"
        "rust-service-application/SKILL.md"
    ): (
        "skills/specification-to-source/backend-application/"
        "rust-service-application/SKILL.md"
    ),
    ("skills/specification-to-source/backend-application/grpc-application/SKILL.md"): (
        "skills/specification-to-source/backend-application/grpc-application/SKILL.md"
    ),
    "skills/specification-to-source/typescript-portable-application/SKILL.md": (
        "skills/specification-to-source/typescript-portable-application/SKILL.md"
    ),
    "skills/specification-to-source/elixir-portable-application/SKILL.md": (
        "skills/specification-to-source/elixir-portable-application/SKILL.md"
    ),
    "skills/specification-to-source/zig-portable-application/SKILL.md": (
        "skills/specification-to-source/zig-portable-application/SKILL.md"
    ),
    "skills/specification-to-source/docker-container-application/SKILL.md": (
        "skills/specification-to-source/docker-container-application/SKILL.md"
    ),
    "skills/specification-to-source/rust-portable-json-application/SKILL.md": (
        "skills/specification-to-source/rust-portable-json-application/SKILL.md"
    ),
    "skills/specification-to-source/swift-portable-json-application/SKILL.md": (
        "skills/specification-to-source/swift-portable-json-application/SKILL.md"
    ),
    "skills/agent/swift-toolchain-prerequisite/SKILL.md": (
        "skills/agent/swift-toolchain-prerequisite/SKILL.md"
    ),
    "skills/specification-to-source/cpp17-portable-json-application/SKILL.md": (
        "skills/specification-to-source/cpp17-portable-json-application/SKILL.md"
    ),
    "skills/agent/author-presentations-and-documents/SKILL.md": (
        "skills/agent/author-presentations-and-documents/SKILL.md"
    ),
    "skills/agent/author-instructional-videos/SKILL.md": (
        "skills/agent/author-instructional-videos/SKILL.md"
    ),
    "skills/agent/record-user-directed-work/SKILL.md": (
        "skills/agent/record-user-directed-work/SKILL.md"
    ),
    "skills/agent/configure-test-workers/SKILL.md": (
        "skills/agent/configure-test-workers/SKILL.md"
    ),
    "skills/agent/verify-frontend-browser/SKILL.md": (
        "skills/agent/verify-frontend-browser/SKILL.md"
    ),
    "skills/agent/ci-test-plan/SKILL.md": "skills/agent/ci-test-plan/SKILL.md",
    "skills/agent/ci-test-plan/shard/SKILL.md": (
        "skills/agent/ci-test-plan/shard/SKILL.md"
    ),
    "skills/agent/ci-test-plan/impact/SKILL.md": (
        "skills/agent/ci-test-plan/impact/SKILL.md"
    ),
    "skills/agent/configure-operator-mcp/SKILL.md": (
        "skills/agent/configure-operator-mcp/SKILL.md"
    ),
    "skills/agent/SKILL.md": "skills/agent/SKILL.md",
    "skills/agent/release-project/SKILL.md": ("skills/agent/release-project/SKILL.md"),
    "skills/agent/release-project/backport/SKILL.md": (
        "skills/agent/release-project/backport/SKILL.md"
    ),
    "skills/agent/release-project/evidence/SKILL.md": (
        "skills/agent/release-project/evidence/SKILL.md"
    ),
    "skills/agent/release-project/advance/SKILL.md": (
        "skills/agent/release-project/advance/SKILL.md"
    ),
    "skills/agent/release-project/ci-status/SKILL.md": (
        "skills/agent/release-project/ci-status/SKILL.md"
    ),
    "skills/agent/release-project/verify-published/SKILL.md": (
        "skills/agent/release-project/verify-published/SKILL.md"
    ),
    "skills/agent/release-project/notify-descendants/SKILL.md": (
        "skills/agent/release-project/notify-descendants/SKILL.md"
    ),
    "skills/agent/associate-release-jira/SKILL.md": (
        "skills/agent/associate-release-jira/SKILL.md"
    ),
    "skills/agent/ingest-channel-work/SKILL.md": (
        "skills/agent/ingest-channel-work/SKILL.md"
    ),
    "skills/agent/develop-in-production-workflow/SKILL.md": (
        "skills/agent/develop-in-production-workflow/SKILL.md"
    ),
    "skills/agent/develop-in-production-workflow/staging/SKILL.md": (
        "skills/agent/develop-in-production-workflow/staging/SKILL.md"
    ),
    "skills/agent/develop-in-production-workflow/staging/dev/SKILL.md": (
        "skills/agent/develop-in-production-workflow/staging/dev/SKILL.md"
    ),
    "skills/agent/develop-in-production-workflow/staging/dev/land/SKILL.md": (
        "skills/agent/develop-in-production-workflow/staging/dev/land/SKILL.md"
    ),
    (
        "skills/agent/develop-in-production-workflow/staging/dev/"
        "survey-peer-work/SKILL.md"
    ): (
        "skills/agent/develop-in-production-workflow/staging/dev/"
        "survey-peer-work/SKILL.md"
    ),
    "skills/agent/write-mac-project-contract/SKILL.md": (
        "skills/agent/write-mac-project-contract/SKILL.md"
    ),
    "skills/agent/write-mac-project-contract/references/mac-repository-contract.md": (
        "skills/agent/write-mac-project-contract/references/mac-repository-contract.md"
    ),
    "workflows/production/staging/dev/workflow.md": (
        "workflows/production/staging/dev/workflow.md"
    ),
    "workflows/sample-host.md": "workflows/sample-host.md",
    "routing/production/staging/dev/routing.json": (
        "routing/production/staging/dev/routing.json"
    ),
    "routing/sample-host.json": "routing/sample-host.json",
}


def _template_flavor_resource_files() -> dict[str, str]:
    """Derive the complete Flavor resource inventory from its taxonomy tree."""

    root = files("literate_ai.project_template").joinpath("flavors")
    pending = [(root, ())]
    discovered: list[str] = []
    while pending:
        current, parent_parts = pending.pop()
        for entry in current.iterdir():
            relative_parts = (*parent_parts, entry.name)
            if entry.is_dir():
                pending.append((entry, relative_parts))
                continue
            resource = PurePosixPath("flavors", *relative_parts).as_posix()
            discovered.append(resource)
    return {resource: resource for resource in sorted(discovered)}


_TEMPLATE_FILES.update(_template_flavor_resource_files())

_STARTER_TEMPLATE_FILES = {
    "samples/hello-component/component.md": "samples/hello-component/component.md",
}


def _default_source_cache() -> SourceCacheConfiguration:
    """Declare both cache targets a project can use, ordered cheapest first.

    The runtime target is operator-bound, disposable, and the sole write target. The
    committed target is project-relative so a repository may check generated source in
    for efficiency; it is read after the runtime one purely because a local hit is
    cheaper, not because either could be stale. A hit requires an exact generation-key
    match, and that key binds the ordered specification set, so changed specifications
    miss everywhere rather than resolving to an older tree.

    The committed target is declared but not created. A project opts in by populating
    it; until then it simply never hits.
    """

    runtime = SourceCacheTarget(
        target_id="standard-local",
        root_kind=SourceCacheRootKind.OPERATOR_BOUND,
        root_reference="standard-local-source-cache",
    )
    committed = SourceCacheTarget(
        target_id="project-committed",
        root_kind=SourceCacheRootKind.PROJECT_RELATIVE,
        root_reference="generated/committed-source-cache",
    )
    return SourceCacheConfiguration(
        mode=SourceCacheMode.READ_WRITE,
        targets=(runtime, committed),
        write_target_id=runtime.target_id,
        require_unique=True,
    )


def _write_starter_acceptance_oracle(target: Path) -> str:
    """Scaffold the starter Component's verifier-owned acceptance oracle.

    Generated source must be independently accepted, so a Component with no oracle
    cannot be built. Shipping one for the starter is what makes `litai init` followed
    by `litai build` work without the operator first authoring expectations.

    It lives under the verifier root and is deliberately not referenced from
    `component.md`: a path to the expected results inside a document the generator
    reads would tell the model exactly where to look.
    """

    lock = json.loads(
        (target / "samples" / "hello-component" / "component.lock.json").read_text(
            encoding="utf-8"
        )
    )
    # The lock stores no per-node revision identity, so match the starter by coordinate.
    specification = next(
        node["revision"]["specification_set_identity"]
        for node in lock["nodes"]
        if node["revision"]["coordinate"]["name"] == "hello-component"
    )
    document = {
        "schema": "literate-ai/component-acceptance-oracle@1",
        "component": "hello-component",
        "specification_set_identity": (
            f"{specification['algorithm']}:{specification['digest']}"
        ),
        "cases": [
            {
                "case_id": "primary",
                "arguments": [
                    {
                        "name": "Ada Lovelace",
                    }
                ],
                "expected_result": {
                    "greeting": "Hello, Ada Lovelace!",
                    "name": "Ada Lovelace",
                },
            },
            {
                "case_id": "generalization",
                "arguments": [
                    {
                        "name": "Grace Hopper",
                    }
                ],
                "expected_result": {
                    "greeting": "Hello, Grace Hopper!",
                    "name": "Grace Hopper",
                },
            },
        ],
    }
    relative = "verification/acceptance/hello-component.json"
    path = target.joinpath(*Path(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return relative


def _require_initializable_target(target: Path) -> None:
    """Admit only harmless repository bootstrap state before scaffolding."""

    unexpected: list[str] = []
    for entry in sorted(target.iterdir(), key=lambda item: item.name):
        if entry.name not in _PRESERVED_INITIALIZATION_ENTRIES:
            unexpected.append(entry.name)
            continue
        if (
            entry.is_symlink()
            or (entry.name == ".git" and not entry.is_dir())
            or (entry.name == "README.md" and not entry.is_file())
        ):
            raise ProjectInitializationError(
                "project.init_target_invalid",
                f"preserved repository bootstrap entry is unsafe: {entry.name}",
            )
    if unexpected:
        preview = ", ".join(unexpected[:8])
        if len(unexpected) > 8:
            preview += f", ... ({len(unexpected) - 8} more)"
        raise ProjectInitializationError(
            "project.init_target_not_empty",
            "project target contains existing content that requires explicit source "
            f"adoption: {preview}; use litai init --convert to adopt an existing "
            "repository",
        )


def _include_template_file(destination: str, active_flavors: frozenset[str]) -> bool:
    """Return True if this template file should be installed given the active Flavors.

    Files under ``flavors/<name>/`` are gated by that Flavor name.  The
    document-pair component and its skill require at least one document-service
    Flavor.  Every other file is cross-cutting and always installed.
    """
    parts = destination.split("/")
    if parts[0] == "flavors" and len(parts) > 1:
        if parts[1] == "os-base":
            return True
        return parts[1] in active_flavors
    if parts[0] == "components" and len(parts) > 1 and parts[1] == "document-pair":
        return bool(active_flavors & _DOCUMENT_PAIR_FLAVORS)
    if (
        parts[0] == "skills"
        and len(parts) > 2
        and parts[1] == "specification-to-source"
        and parts[2] == "document-pair"
    ):
        return bool(active_flavors & _DOCUMENT_PAIR_FLAVORS)
    if (
        parts[0] == "skills"
        and len(parts) > 2
        and parts[1] == "agent"
        and parts[2] == "swift-toolchain-prerequisite"
    ):
        return bool(
            active_flavors
            & {
                "lang-swift",
                "toolchain-swift-apple",
                "toolchain-swift-linux",
                "toolchain-swift-windows",
            }
        )
    if destination.startswith(
        "skills/specification-to-source/generate-nvidia-cuda-application/"
    ) or destination.startswith("skills/agent/select-nvidia-accelerated-stack/"):
        return "accel-nvidia-cuda" in active_flavors
    if destination.startswith(
        "skills/specification-to-source/docker-container-application/"
    ):
        return "deploy-docker" in active_flavors
    return True


StandardInitializationBinding = tuple[
    StandardProjectLifecycleDriver,
    ProjectTestReceiptPolicy,
]
StandardBindingProvider = Callable[[], StandardInitializationBinding | None]
RepositoryLineageResolver = Callable[[RepositoryParentSelection], RepositoryLineage]
RepositoryCatalogPlanner = Callable[[RepositoryLineage], InheritedCatalogPlan]


def _source_checkout() -> bool:
    root = Path(__file__).resolve().parents[3]
    return (root / "pyproject.toml").is_file() and (root / ".git").exists()


def _default_parent_repository_url(origin: ProjectInitializationOrigin) -> str:
    if _source_checkout():
        checkout = Path(__file__).resolve().parents[3]
        return repository_parent_reference(str(checkout)).repository_url
    return repository_parent_reference(origin.repository_url).repository_url


def _default_release_tag_lister(repository_url: str) -> tuple[str, ...]:
    configured = os.environ.get("OBJ_DIR")
    object_root = (
        Path(configured).expanduser() if configured else Path.cwd() / "_build"
    ).resolve()
    return GitRepositorySnapshotProvider(
        object_root / "repository-lineage"
    ).list_remote_tags(repository_url)


def _default_repository_parent(
    origin: ProjectInitializationOrigin,
    *,
    release_tags: Callable[[str], tuple[str, ...]] | None = None,
) -> RepositoryParentSelection:
    url = _default_parent_repository_url(origin)
    lister = release_tags or _default_release_tag_lister
    try:
        tags = lister(url)
    except GitRepositoryLineageError as exc:
        raise ProjectInitializationError(exc.code, exc.message) from exc
    if not isinstance(tags, tuple) or any(not isinstance(item, str) for item in tags):
        raise ProjectInitializationError(
            "repository_lineage.tags_invalid",
            "release-tag lister returned an invalid result",
        )
    revision = highest_release_tag(tags, at_most=DISTRIBUTION_VERSION)
    if revision is None:
        raise ProjectInitializationError(
            "repository_lineage.release_unavailable",
            "no published vX.Y.Z release tag at or below the installed CLI version "
            f"{DISTRIBUTION_VERSION} is available as the default parent; pass "
            "--from URL#REVISION to select a parent explicitly",
        )
    return RepositoryParentSelection.inherit(
        (RepositoryParentReference(url, revision),)
    )


def _default_repository_lineage_resolver(
    selection: RepositoryParentSelection,
) -> RepositoryLineage:
    configured = os.environ.get("OBJ_DIR")
    object_root = (
        Path(configured).expanduser() if configured else Path.cwd() / "_build"
    ).resolve()
    return resolve_repository_lineage(
        selection,
        GitRepositorySnapshotProvider(object_root / "repository-lineage"),
    )


def _default_repository_catalog_planner(
    lineage: RepositoryLineage,
) -> InheritedCatalogPlan:
    configured = os.environ.get("OBJ_DIR")
    object_root = (
        Path(configured).expanduser() if configured else Path.cwd() / "_build"
    ).resolve()
    provider = GitRepositorySnapshotProvider(object_root / "repository-lineage")
    return plan_inherited_catalogs(lineage, provider)


def _installed_standard_binding() -> StandardInitializationBinding | None:
    """Bind production init to exact wheel bytes; source checkouts stay explicit."""

    try:
        distribution = observe_installed_framework_distribution()
        policy = load_current_standard_lifecycle_policy()
    except StandardLifecycleBindingError:
        if _source_checkout():
            return None
        raise
    driver = StandardProjectLifecycleDriver(
        distribution.identity,
        policy.identity,
    )
    receipt = ProjectTestReceiptPolicy(
        policy.policy_id,
        policy.policy_version,
        driver.identity,
        policy.required_evidence_kinds,
        policy.minimum_test_count,
    )
    return driver, receipt


_STARTER_DOCS = {
    "docs/README.md": """# Project guide

<!-- DOC-IDENTITY: Replace the quoted placeholder below with a short description
     of what this project is and does. This is the front door for anyone reading
     the documentation. -->

> **Replace this paragraph** with a one-to-three sentence description of your
> project: what it is, what it does, and who it is for.

Start with [getting started](user/getting-started.md) for installation and usage.
See [active work](roadmap/active-work.md) for current development status.

## Development

This project is built with [Literate AI](https://github.com/jordanhubbard/literate-ai).
The [framework flow](user/framework-flow.md) explains the specification-led lifecycle,
and the [project map](user/project-layout.md) identifies the authority for a change.

```mermaid
flowchart LR
    Spec[Component specification] --> Plan[Resolved plan]
    Flavor[Selected Flavors] --> Plan
    Parent[Exact repository ancestor DAG] --> Plan
    Default[Removable +make preference] -.-> Plan
    Skill[Exact skills] --> Plan
    Plan --> Generate[Disposable source]
    Generate --> Verify[Validate, build, and test]
```

See [readable specifications](user/specifications.md),
[models and generation](user/models-and-generation.md),
[private test matrices](user/test-matrix.md),
[security](user/security.md), [skill boundaries](architecture/skills.md), and the
[authority learning loop](architecture/authority-learning-loop.md), the
[mission-specification map](architecture/mission-specification-composition.md), and the
[traceability rule](architecture/design-traceability.md) when those concerns apply.

Initialize from an organization or product repository with
`litai init PATH --from URL[#REVISION]`. Literate AI resolves every ancestor without
executing repository code, then records exact commits and inherited catalog provenance.
Use `litai update` to re-resolve that chain and `litai reparent URL|none` to review an
explicit parent change.
""",
    "docs/user/getting-started.md": """# Getting started

[Project guide](../README.md) → getting started

<!-- DOC-IDENTITY: Replace this section with your project's own description.
     A downstream project's getting-started document must describe the project
     itself — what it is, what it does, how to install and use it — not the
     framework that built it. Delete this comment block and the placeholder
     sections below, replacing them with your actual product documentation.
     The "Development workflow" section at the bottom may be kept as-is for
     contributors. -->

## What is this project?

> **Replace this section.** Describe what this project is, what problem it solves,
> and who it is for. A reader should understand the project's purpose without
> knowing anything about Literate AI.

## Installation

> **Replace this section.** Document the exact artifact or package to obtain,
> installation destination, supported host prerequisites, complete non-secret
> configuration, environment-backed credentials, persistence and network
> assumptions, startup order, health/readiness checks, one verified request with
> its expected result, upgrade or rollback, and uninstall or cleanup.
>
> Contributor-only `litai rebuild` commands and internal smoke harnesses do not
> count as installation. If packaging or service registration is not implemented,
> say so prominently and link the active work that owns that gap rather than
> inventing commands.

## Usage

> **Replace this section.** Show how a user interacts with the installed project:
> representative commands, API calls, configuration, or UI workflows.

---

## Development workflow

This project uses [Literate AI](https://github.com/jordanhubbard/literate-ai) to
keep specifications as durable authority and generate source, current tests, and a
CycloneDX source SBOM into a disposable workspace. The commands below are for
contributors, not end users.

Validate the project and inspect the exact recipe (planning does not invoke a
model or execute generated code):

```console
litai project validate
litai lock --check
litai plan samples/hello-component
```

Every non-empty initialized project begins with a portable hello Component. With an
authenticated coding CLI and the selected host toolchain, prove the complete local
lifecycle before changing it:

```console
litai rebuild samples/hello-component --project . \\
  --allow-host-execution --update-receipt
```

The rebuild generates source and current tests from the specification, builds a
runnable artifact, runs both generated and independent acceptance tests, executes the
application, and commits the compact current passing receipt. Modify
`samples/hello-component/component.md` to begin the first application, or use
`litai init --empty` when no starter is wanted.

Invoke the `Execute:` command printed by rebuild with `{"name":"LitAI"}` as its one
argument. The known output is exactly
`{"greeting":"Hello, LitAI!","name":"LitAI"}`.

```mermaid
flowchart LR
    Spec[Specification] --> Recipe((Exact recipe))
    Flavor[Selected Flavors] --> Recipe
    Skill[Pinned skills] --> Recipe
    Workflow[Workflow] --> Recipe
    Route[Routing] --> Recipe
    Recipe --> Source[Disposable source + tests + SBOM]
    Source --> Build[Authorized build and verification]
```

`+flavor` selects a variation and `-flavor` removes one. Explicit Component and
Flavor requirements outrank defaults, so `-bazel` removes the scaffold's Bazel
preference before prompt assembly. Read the [framework flow](framework-flow.md) before
adding a lifecycle driver that compiles or runs generated source, and use the
[project map](project-layout.md) to change the owning artifact.
""",
    "docs/user/specifications.md": """# Writing readable specifications

[Project guide](../README.md) → readable specifications

Use specifications for observable product behavior, Flavors for OS/language/build
choices, and exact skills for reusable conversion guidance. Start every normal Component
with one `component.md`: strict frontmatter carries portable Component metadata and the
Markdown body is its default `literate-markdown` behavioral specification.

```markdown
---
namespace: example
version: 1.0.0
display_name: Scene Viewer
profiles: []
sample: false
provides: []
requires: []
authoring_inputs: []
workflow_definition:
  uri: ../../workflows/production/staging/dev/workflow.md
routing_policy:
  uri: ../../routing/production/staging/dev/routing.json
flavor_slots: []
entrypoints: []
acceptance_contracts: []
source_dependencies: []
---
# Scene Viewer

Load one reviewed scene and expose clear failures for missing or invalid assets.
```

Do not create `component.json`, `openspec/app.json`, `openspec/spec.md`, or an
`acceptance/` directory as peer authoring files for a simple Component. Repository test
vectors and private oracles belong outside the Component tree. Add another specification
or interface document only for a named domain, module, protocol, or independently
consumed public Component boundary.

An additional `literate-markdown` node begins with narrow frontmatter:

```markdown
---
name: Scene loading
summary: Load and validate one factory scene
kind: component
references:
  - product.core.scene-contract
---
```

Only `name`, `summary`, and `kind` are required. Paths derive IDs and parents;
`references` imports local constraints. Do not add dates, hand-bumped revisions,
toolchains, or generic test/build guidance. Content identities and Git own history;
Flavors and skills own reusable technique. When explicit additional roots are selected,
the first is still `component.md`; nested node folders use their own `spec.md` only when
they represent a genuine named boundary.

Run `litai plan COMPONENT ...` to validate the exact corpus and inspect its
deterministic context graph before generation. See the
[mission-specification map](../architecture/mission-specification-composition.md) for
the distinction between local node nesting and independently generatable Components.
""",
    "docs/user/framework-flow.md": """# Framework flow

[Project guide](../README.md) → framework flow

Specifications define behavior; selected Flavors add target requirements; exact skills
guide conversion; workflows and routing constrain model work. Generated source is
disposable and lives outside the project. The scaffold's `+bazel` selection is only a
removable prompt preference: explicit specification requirements and selected Flavors
take precedence, and `-bazel` removes it before prompt assembly.

```mermaid
flowchart TD
    Read[Read specifications] --> Plan[litai plan]
    Plan --> Generate[litai generate]
    Generate --> Validate[Validate and classify]
    Validate --> Authorize{Authorized?}
    Authorize -- yes --> Build[Build]
    Build --> Test[Test known behavior]
    Authorize -- no --> Stop[Stop safely]
```

Use the [project map](project-layout.md) to change the owning artifact, and read
[security](security.md) before compiling or running generated code.
""",
    "docs/user/project-layout.md": """# Project layout

[`literate.project.json`](../../literate.project.json) names every authoritative root,
including `documentation_roots`; nearby directories are ambient until declared. Keep
the provider-neutral onboarding skill at root `SKILL.md` and make provider files thin
pointers to it.

The manifest declares source-intelligence policy as provider `none` with every
stage off. Literate AI does not ship or invoke a source-graph indexer.

Repository inheritance is separate from Git remotes. `.literate/repository-parent.json`
records the direct parent selection, `.literate/repository-lineage.json` locks the
complete exact ancestor DAG, and `.literate/imports.json` records inherited Component,
Flavor, and skill files. `litai update` re-resolves that chain; `litai reparent none`
explicitly makes the project a root.

Component specifications may place explanatory Mermaid diagrams beside their prose so
behavior and its illustration evolve together. Diagrams explain; prose requirements and
acceptance scenarios remain normative. See the
[traceability rule](../architecture/design-traceability.md) and
[framework flow](framework-flow.md).

A normal Component keeps portable metadata and its default behavior together in
`components/NAME/component.md`. Generated `component.lock.json` records exact target and
dependency resolution beside it but is not hand-maintained prose. Extra specification or
interface files are exceptional named boundaries, not boilerplate for every Component.
Harness vectors and private expected-value oracles live outside Component authority.

The initializer installs the content-pinned `flavors/build-bazel/` policy and its exact
`skills/specification-to-source/bazel-build-system/` input. The manifest selects it by
default only when a Component declares a compatible `build.system` slot. It is not
runtime enforcement or evidence that Bazel actually built an application.
""",
    "docs/architecture/skills.md": """# Skills

[Project guide](../README.md) → skill boundaries

The root `SKILL.md` teaches an agent how to use the project. It is not a generation
input. Specification-to-source skills live under `skills/specification-to-source/` and
enter generation only through exact `ContentReference` pins on Components or Flavors.
Each skill dependency also pins the required skill version and content identity.

The installed `bazel-build-system` skill is reached through the pinned `bazel` Flavor.
It recommends Bazel in the generation prompt but yields to explicit Component and
selected-Flavor requirements. Selecting an alternative build-system Flavor or using
`-bazel` removes that skill from the resolved generation recipe.

Inverse source-to-specification skills are a separate catalog. Return to the
[framework flow](../user/framework-flow.md) before generating source.
""",
    "docs/architecture/authority-learning-loop.md": """# Authority learning loop

[Project guide](../README.md) → authority learning loop

A project learns only when typed run evidence produces a reviewed Git-tracked change to
future generation authority. Retry feedback repairs one candidate; it is not durable
learning by itself.

```mermaid
flowchart LR
    Run[Derivation evidence] --> Decide{Reusable lesson?}
    Decide -- no --> History[Compact run history]
    Decide -- yes --> Classify{Narrowest owner}
    Classify --> Spec[Component behavior or interface]
    Classify --> Flavor[Target variance]
    Classify --> Skill[Reusable conversion technique]
    Classify --> Flow[Workflow or routing]
    Spec & Flavor & Skill & Flow --> Review[Review, refresh locks, rebuild, accept]
    Review --> Git[Git-tracked authority]
```

Never copy a generated workaround into a behavioral specification merely because that
is where the failure appeared. Private acceptance values, secrets, host paths, and prior
candidate source cannot enter a learning proposal. Skill changes must also pass the
project's pinned SkillEvaluator gate. Until `litai learn` is implemented, make this
classification explicitly during review and bind the accepted lesson through the normal
authority diff, lock refresh, lifecycle, and Git commit.
""",
    (
        "docs/architecture/mission-specification-composition.md"
    ): """# Mission specification composition

[Project guide](../README.md) → mission specification composition

Large products use three different relations:

```mermaid
flowchart LR
    H[Local spec-node hierarchy] --> C[One bounded Component context]
    R[Explicit local references] --> C
    C --> G[Generated Component]
    G -->|public capability contract| A[Application Component]
    F[Selected Flavors] --> G
    S[Exact skills] --> G
```

A local Markdown hierarchy supplies reading context. A Component capability edge joins
independently generated, cached, built, tested, versioned, and published units. Shared
OS, language, frontend strategy, packaging, and build-system choices are Flavors;
conversion conventions are skills. Do not turn either into duplicated “constraint
specs.” Only a direct dependency's public interface crosses a consumer's generation
boundary.

An agent may generate glue needed to implement selected interfaces, but observable new
behavior requires a reviewed spec change. Structural ambiguity and reference cycles
fail deterministically; natural-language contradiction is surfaced by the pinned
planning skill and resolved through human review rather than hidden by “nearest wins.”
""",
    "docs/user/models-and-generation.md": """# Models and generation

[Project guide](../README.md) → models and generation

Run `litai plan COMPONENT --flavor=+NAME` before generation. When two Component
slots share an axis, bind each role explicitly with `--flavor=+SLOT:NAME` instead.
Project defaults run first; explicit selectors run afterward, so an explicit
build-system Flavor replaces the scaffold's `+bazel` preference and `--flavor=-bazel`
removes it without replacement.
`CODING_CLI` may select `codex`, `claude`, `cursor-agent`, or `opencode`;
otherwise Literate AI chooses the first command on `PATH` in that order. A model
declared by an exact specification or Flavor is passed to the selected CLI using
that CLI's supported model-selection argument. Before an OpenCode model call, LitAI
requires its bounded `--pure run --help` capability surface and reports
`coding_cli.incompatible` with upgrade guidance rather than weakening pure mode.
Generation collects canonical source, then continues through guarded
validation/build/test. Continue through the
[framework flow](framework-flow.md).
""",
    "docs/user/security.md": """# Security

[Project guide](../README.md) → security

Treat generated source as untrusted. Keep generation output outside the project,
require an empty output directory, inspect and validate the exact tree, and require
explicit build and host-execution authorization before compiling or running it.

The [framework flow](framework-flow.md) makes those gates visible.
""",
    "docs/architecture/design-traceability.md": """# Design traceability

<!-- literate-ai:authority-review-pending -->

[Project guide](../README.md) → design traceability

Behavior belongs in Component specifications; target variance belongs in Flavors;
conversion technique belongs in exact skills; execution order belongs in workflows;
model eligibility belongs in routing; validation and authorization remain framework
policy. The manifest selects the source-intelligence provider, while its local database
remains
derived evidence outside source authority. Changes should update the owning artifact,
its nearby explanation or diagram,
and an end-to-end test. Illustrations aid understanding; prose requirements and
acceptance scenarios remain normative. See the [project map](../user/project-layout.md).
""",
}

LEGACY_LIFT_SHIFT_ADR = "docs/decisions/0001-legacy-project-lift-and-shift.md"
_LEGACY_LIFT_SHIFT_LINK = (
    "- [Legacy project lift-and-shift decision]"
    "(decisions/0001-legacy-project-lift-and-shift.md)"
)


def render_starter_document(destination: str, *, converted: bool) -> str:
    """Render the initialized project variant shared by init and update."""

    content = _STARTER_DOCS[destination]
    if (
        converted
        and destination == "docs/README.md"
        and _LEGACY_LIFT_SHIFT_LINK not in content
    ):
        return (
            content.rstrip("\n")
            + "\n\n## Adoption decisions\n\n"
            + _LEGACY_LIFT_SHIFT_LINK
            + "\n"
        )
    return content


class ProjectInitializationError(ValueError):
    """Stable canonical-project initialization or review-recording failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _initialization_error(error: BaseException) -> NoReturn:
    raise ProjectInitializationError(
        str(getattr(error, "code", "project.init_failed")),
        str(getattr(error, "message", "project initialization failed")),
    ) from error


def _project_id(target: Path, configured: str | None) -> str:
    value = configured or target.name
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")
    if not normalized:
        raise ProjectInitializationError(
            "project.invalid_id", "project ID must contain a letter or number"
        )
    return normalized


def _template_text(relative: str) -> str:
    return (
        files("literate_ai.project_template")
        .joinpath(*Path(relative).parts)
        .read_text(encoding="utf-8")
    )


def _validate_template_skill_catalog(active_flavors: frozenset[str]) -> None:
    documents = {
        destination: _template_text(resource).encode("utf-8")
        for destination, resource in _TEMPLATE_FILES.items()
        if (destination == "SKILL.md" or destination.startswith("skills/"))
        and _include_template_file(destination, active_flavors)
    }
    try:
        AgentSkillCatalog.from_documents(
            documents,
            validate_dependencies=True,
            validate_references=True,
        )
    except AgentSkillCatalogError as exc:
        raise ProjectInitializationError(exc.code, exc.message) from exc


def _source_intelligence_policy(provider_id: str) -> ProjectSourceIntelligencePolicy:
    if provider_id not in {"none", "codegraph-cli"}:
        raise ProjectInitializationError(
            "project.init_source_intelligence_unsupported",
            "source-intelligence provider must be none or codegraph-cli",
        )
    enabled = provider_id == "codegraph-cli"
    return ProjectSourceIntelligencePolicy(
        provider_id=provider_id,
        command="codegraph" if enabled else None,
        minimum_version="1.1.1" if enabled else None,
        artifact_path=".codegraph/codegraph.db" if enabled else None,
        stages=(
            (
                (
                    SourceIntelligenceStage.PROJECT_MAINTENANCE,
                    SourceIntelligenceMode.PREFERRED,
                ),
                (
                    SourceIntelligenceStage.SOURCE_GENERATION,
                    SourceIntelligenceMode.PREFERRED,
                ),
                (
                    SourceIntelligenceStage.CACHE_CONSUMPTION,
                    SourceIntelligenceMode.OFF,
                ),
                (
                    SourceIntelligenceStage.SOURCE_TO_SPECIFICATION,
                    SourceIntelligenceMode.REQUIRED,
                ),
                (
                    SourceIntelligenceStage.REPOSITORY_SOURCE_ADMISSION,
                    SourceIntelligenceMode.OFF,
                ),
                (
                    SourceIntelligenceStage.STRUCTURAL_REVIEW,
                    SourceIntelligenceMode.PREFERRED,
                ),
            )
            if enabled
            else tuple(
                (stage, SourceIntelligenceMode.OFF) for stage in SourceIntelligenceStage
            )
        ),
        artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
    )


InitializationOriginProvider = Callable[[], ProjectInitializationOrigin]


def _git_value(root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 16 * 1024:
        return None
    value = completed.stdout.strip()
    return value or None


def _git_origin(root: Path) -> ProjectInitializationOrigin | None:
    selected = root if root.is_dir() else root.parent
    checkout = _git_value(selected, "rev-parse", "--show-toplevel")
    if checkout is None:
        return None
    checkout_root = Path(checkout).resolve(strict=True)
    repository_url = _git_value(checkout_root, "remote", "get-url", "origin")
    revision = _git_value(checkout_root, "rev-parse", "--verify", "HEAD^{commit}")
    if repository_url is None or revision is None:
        return None
    try:
        return ProjectInitializationOrigin(
            repository_url=repository_url,
            git_revision=revision.casefold(),
            distribution_name="literate-ai",
            distribution_version=DISTRIBUTION_VERSION,
        )
    except ValueError:
        return None


def _installed_direct_url() -> dict[str, Any] | None:
    try:
        raw = metadata.distribution("literate-ai").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if raw is None or len(raw.encode("utf-8")) > 64 * 1024:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _installed_distribution_origin() -> ProjectInitializationOrigin | None:
    try:
        return observe_installed_framework_origin(DISTRIBUTION_VERSION)
    except StandardLifecycleBindingError:
        return None


def _direct_url_checkout(value: dict[str, Any]) -> Path | None:
    url = value.get("url")
    directory = value.get("dir_info")
    if not isinstance(url, str) or not isinstance(directory, dict):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        return None
    converted = url2pathname(unquote(parsed.path))
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        converted = f"//{parsed.netloc}{converted}"
    checkout = Path(converted)
    return checkout if checkout.exists() else None


def _direct_url_vcs_origin(
    value: dict[str, Any],
) -> ProjectInitializationOrigin | None:
    url = value.get("url")
    vcs = value.get("vcs_info")
    if not isinstance(url, str) or not isinstance(vcs, dict) or vcs.get("vcs") != "git":
        return None
    revision = vcs.get("commit_id")
    if not isinstance(revision, str):
        return None
    try:
        return ProjectInitializationOrigin(
            repository_url=url,
            git_revision=revision.casefold(),
            distribution_name="literate-ai",
            distribution_version=DISTRIBUTION_VERSION,
        )
    except ValueError:
        return None


def discover_installed_initialization_origin(
    *,
    source_roots: tuple[Path, ...] | None = None,
    direct_url: dict[str, Any] | None = None,
) -> ProjectInitializationOrigin:
    """Resolve one exact Git origin without substituting a canonical repository."""

    # A wheel's embedded origin is part of the observed, immutable distribution
    # payload.  ``direct_url.json`` describes how pip transported that wheel (often a
    # disposable local checkout), while a surrounding Git worktree describes the
    # caller's current directory.  Neither is competing framework authority once the
    # built distribution contains its exact origin.  Treating those locator records as
    # peers makes a correctly attested wheel ambiguous whenever it was installed from a
    # clean projection whose ``origin`` URL differs from the published repository URL.
    embedded = _installed_distribution_origin()
    if embedded is not None:
        return embedded

    document = _installed_direct_url() if direct_url is None else direct_url
    roots = list(source_roots or (Path(__file__).resolve(),))
    if document is not None:
        checkout = _direct_url_checkout(document)
        if checkout is not None:
            roots.append(checkout)
    candidates = [origin for root in roots if (origin := _git_origin(root)) is not None]
    if document is not None:
        vcs_origin = _direct_url_vcs_origin(document)
        if vcs_origin is not None:
            candidates.append(vcs_origin)
    unique = {
        (item.repository_url, item.git_revision, item.distribution_version): item
        for item in candidates
    }
    if not unique:
        raise ProjectInitializationError(
            "project.init_origin_unavailable",
            "the installed Literate-AI distribution has no exact Git origin "
            "and revision",
        )
    if len(unique) != 1:
        raise ProjectInitializationError(
            "project.init_origin_ambiguous",
            "the installed Literate-AI distribution resolves to multiple Git origins",
        )
    return next(iter(unique.values()))


def _initialization_baseline(
    root: Path,
    origin: ProjectInitializationOrigin,
    created: list[str],
) -> ProjectInitializationBaseline:
    records: list[ProjectInitializationBaselineFile] = []
    for relative in sorted(set(created)):
        path = root.joinpath(*Path(relative).parts)
        if path.is_symlink() or not path.is_file():
            raise ProjectInitializationError(
                "project.init_baseline_file_invalid",
                f"initialized framework-owned file is not regular: {relative}",
            )
        content = path.read_bytes()
        if len(content) > _MAXIMUM_INITIALIZED_FILE_BYTES:
            raise ProjectInitializationError(
                "project.init_baseline_file_oversized",
                f"initialized framework-owned file exceeds the size limit: {relative}",
            )
        records.append(
            ProjectInitializationBaselineFile(
                relative,
                len(content),
                ContentIdentity(
                    HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()
                ),
            )
        )
    return ProjectInitializationBaseline(
        origin_identity=origin.identity,
        template_protocol=PROJECT_TEMPLATE_PROTOCOL,
        files=tuple(records),
    )


def _atomic_replace(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    stream = None
    try:
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        fchmod = getattr(os, "fchmod", None)
        if fchmod is None:
            # Windows exposes chmod(path, mode), but not descriptor-based chmod.
            # The temporary is a freshly created regular file in the already
            # validated parent, so changing it by its exact private path retains
            # the atomic-replacement boundary.
            os.chmod(temporary, mode)
        else:
            fchmod(descriptor, mode)
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        stream = None
        os.replace(temporary, path)
    finally:
        if stream is not None:
            stream.close()
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class FilesystemProjectInitializationAdapter:
    """Create canonical projects without depending on CLI-private argument state."""

    def __init__(
        self,
        *,
        validation: FilesystemProjectValidationAdapter | None = None,
        standard_binding_provider: StandardBindingProvider = (
            _installed_standard_binding
        ),
        initialization_origin_provider: InitializationOriginProvider = (
            discover_installed_initialization_origin
        ),
        repository_lineage_resolver: RepositoryLineageResolver = (
            _default_repository_lineage_resolver
        ),
        repository_catalog_planner: RepositoryCatalogPlanner = (
            _default_repository_catalog_planner
        ),
    ) -> None:
        self._validation = validation or FilesystemProjectValidationAdapter()
        self._standard_binding_provider = standard_binding_provider
        self._initialization_origin_provider = initialization_origin_provider
        self._repository_lineage_resolver = repository_lineage_resolver
        self._repository_catalog_planner = repository_catalog_planner

    def initialize(
        self,
        target: Path,
        *,
        flavor_selectors: tuple[str, ...] | None = None,
        project_id: str | None = None,
        profile: str = "canonical",
        source_intelligence_provider: str = "none",
        empty: bool = False,
        project_type: str | None = None,
        bootstrap_tools: bool = True,
        convert: bool = False,
        run_baseline: bool = False,
        baseline_timeout_seconds: int = HARNESS_COMMAND_TIMEOUT_SECONDS,
        baseline_diagnostic_chars: int = HARNESS_DIAGNOSTIC_CHARS,
        harness_workspace_links: tuple[HarnessWorkspaceLink, ...] = (),
        allow_unready: bool = False,
        parent_selection: RepositoryParentSelection | None = None,
        default_branch: str | None = None,
        root_plan: Path | None = None,
    ) -> dict[str, Any]:
        try:
            baseline_timeout_seconds = validate_harness_command_timeout(
                baseline_timeout_seconds
            )
        except ValueError as exc:
            raise ProjectInitializationError(
                "project.convert_timeout_invalid", str(exc)
            ) from exc
        try:
            baseline_diagnostic_chars = validate_harness_diagnostic_limit(
                baseline_diagnostic_chars
            )
        except ValueError as exc:
            raise ProjectInitializationError(
                "project.convert_diagnostic_limit_invalid", str(exc)
            ) from exc
        resolved_project_type = project_type or DEFAULT_PROJECT_TYPE
        if resolved_project_type not in PROJECT_TYPES:
            raise ProjectInitializationError(
                "project.init_type_unknown",
                "unknown project type "
                + repr(resolved_project_type)
                + "; known types: "
                + ", ".join(sorted(PROJECT_TYPES)),
            )
        if flavor_selectors is None:
            if convert:
                flavor_selectors = apply_init_flavor_defaults(
                    detect_repo_flavors(Path(target))
                )
            else:
                flavor_selectors = (
                    "+flavor://literate-ai/build-make",
                    "+flavor://literate-ai/lang-python",
                    "+flavor://literate-ai/package-pip",
                    host_platform_selector(),
                )
        try:
            flavor_selectors = tuple(
                canonical_flavor_selector("+" + selector.removeprefix("+"))
                for selector in flavor_selectors
            )
        except ValueError as exc:
            raise ProjectInitializationError(
                "project.init_flavor_unknown",
                str(exc),
            ) from exc
        if not flavor_selectors:
            raise ProjectInitializationError(
                "project.init_flavor_required",
                "project initialization requires at least one explicit Flavor selector",
            )
        invalid_selectors = tuple(
            selector
            for selector in flavor_selectors
            if not isinstance(selector, str)
            or not selector.startswith("+")
            or selector.removeprefix("+") not in KNOWN_FLAVOR_SELECTORS
        )
        if invalid_selectors:
            raise ProjectInitializationError(
                "project.init_flavor_unknown",
                "project initialization received unknown or non-additive Flavor "
                "selectors: " + ", ".join(repr(item) for item in invalid_selectors),
            )
        active_flavor_axes = {
            KNOWN_FLAVOR_SELECTORS[selector.removeprefix("+")]
            for selector in flavor_selectors
        }
        missing_starter_axes = (
            {"implementation.language-ecosystem", "platform.os"} - active_flavor_axes
            if not empty
            else set()
        )
        if missing_starter_axes:
            raise ProjectInitializationError(
                "project.init_flavor_axis_required",
                "the starter application requires explicit Flavor selections for: "
                + ", ".join(sorted(missing_starter_axes)),
            )
        configured = Path(target)
        if configured.is_symlink():
            raise ProjectInitializationError(
                "project.init_target_invalid",
                "project target must be a new or empty regular directory",
            )
        resolved_target = configured.resolve()
        quarantine: dict[str, object] | None = None
        interrupted = False
        convert_readiness: dict[str, object] | None = None
        monorepo_staging: tempfile.TemporaryDirectory[str] | None = None
        monorepo_bundle: Path | None = None
        monorepo_manifest: dict[str, object] | None = None
        monorepo_receipts: dict[str, Path] = {}
        monorepo_components_document: dict[str, object] | None = None
        repository_policy = RepositoryPolicy()
        if default_branch is not None:
            repository_policy, _repository_default_branch = (
                _conversion_repository_policy(resolved_target, default_branch)
            )
        if resolved_target.exists() and not resolved_target.is_dir():
            raise ProjectInitializationError(
                "project.init_target_invalid",
                "project target must be a new or empty regular directory",
            )
        if harness_workspace_links and not convert:
            raise ProjectInitializationError(
                "harness.workspace_links_require_convert",
                "harness workspace links require conversion",
            )
        if root_plan is not None and not convert:
            raise ProjectInitializationError(
                "project.root_plan_requires_convert",
                "a refined root plan requires conversion",
            )
        if root_plan is not None and not run_baseline:
            raise ProjectInitializationError(
                "project.convert_monorepo_baseline_required",
                "refined monorepo conversion requires explicit baseline execution",
            )
        if convert:
            # Adopting an existing repository: inspect in place first. Mutate only
            # when the catalog can wrap the detected operations (or the operator
            # explicitly records the gap with allow_unready).
            if not resolved_target.is_dir():
                raise ProjectInitializationError(
                    "project.init_target_invalid",
                    "convert target must be an existing project directory",
                )
            try:
                validate_harness_workspace_links(
                    harness_workspace_links, project_root=resolved_target
                )
            except HarnessWorkspaceError as exc:
                raise ProjectInitializationError(exc.code, exc.message) from exc
            if (resolved_target / PROJECT_FILENAME).exists():
                raise ProjectInitializationError(
                    "project.already_initialized",
                    f"{PROJECT_FILENAME} already exists; this project is already a "
                    "literate-ai project and does not require conversion, though it "
                    "may require an update (litai update)",
                )
            convert_readiness = plan_convert(
                resolved_target,
                default_branch=default_branch,
                root_plan=root_plan,
            )
            repository_policy, _repository_default_branch = (
                _conversion_repository_policy(resolved_target, default_branch)
            )
            if (
                convert_readiness["readiness"] == "blocked-missing-catalog"
                and not allow_unready
            ):
                gaps = convert_readiness.get("catalog_gaps") or []
                preview = ", ".join(
                    str(item.get("selector", item))
                    if isinstance(item, dict)
                    else str(item)
                    for item in gaps
                )
                raise ProjectInitializationError(
                    "project.convert_not_ready",
                    "conversion is blocked until missing catalog entries are authored"
                    + (f": {preview}" if preview else ""),
                )
            interrupted = resolved_target.joinpath(
                *Path(INITIALIZATION_BASELINE_FILE).parts
            ).is_file()
        elif resolved_target.exists():
            _require_initializable_target(resolved_target)
        origin = self._initialization_origin_provider()
        if not isinstance(origin, ProjectInitializationOrigin):
            raise ProjectInitializationError(
                "project.init_origin_invalid",
                "initialization origin provider returned an invalid contract",
            )
        selection = parent_selection or _default_repository_parent(origin)
        if not isinstance(selection, RepositoryParentSelection):
            raise ProjectInitializationError(
                "repository_lineage.selection_invalid",
                "project initialization requires typed repository-parent authority",
            )
        try:
            repository_lineage = self._repository_lineage_resolver(selection)
        except (
            GitRepositoryLineageError,
            RepositoryLineageResolutionError,
            ValueError,
        ) as exc:
            raise ProjectInitializationError(
                str(getattr(exc, "code", "repository_lineage.resolve_failed")),
                str(
                    getattr(exc, "message", "repository lineage could not be resolved")
                ),
            ) from exc
        if (
            not isinstance(repository_lineage, RepositoryLineage)
            or repository_lineage.selection != selection
        ):
            raise ProjectInitializationError(
                "repository_lineage.resolution_invalid",
                "repository-lineage resolver returned inconsistent evidence",
            )
        inherited_catalogs = InheritedCatalogPlan(repository_lineage, ())
        if parent_selection is not None and repository_lineage.nodes:
            try:
                inherited_catalogs = self._repository_catalog_planner(
                    repository_lineage
                )
            except RepositoryCatalogError as exc:
                raise ProjectInitializationError(exc.code, exc.message) from exc
            if (
                not isinstance(inherited_catalogs, InheritedCatalogPlan)
                or inherited_catalogs.lineage != repository_lineage
            ):
                raise ProjectInitializationError(
                    "repository_catalog.plan_invalid",
                    "repository catalog planner returned inconsistent evidence",
                )
        source_intelligence = _source_intelligence_policy(source_intelligence_provider)
        del bootstrap_tools
        tool_bootstrap: dict[str, object] = {
            "state": "disabled",
            "reason": "no-source-intelligence-provider",
        }
        prerequisites = _initialization_prerequisites()
        try:
            standard_binding = self._standard_binding_provider()
        except StandardLifecycleBindingError as exc:
            raise ProjectInitializationError(exc.code, exc.message) from exc
        lifecycle_driver = None if standard_binding is None else standard_binding[0]
        receipt_policy = None if standard_binding is None else standard_binding[1]
        definition = ProjectDefinition(
            project_id=_project_id(resolved_target, project_id),
            version="1.0.0",
            profile=profile,
            agent_skill="SKILL.md",
            component_roots=("components", "samples"),
            flavor_roots=("flavors",),
            skill_roots=("skills",),
            workflow_roots=("workflows",),
            routing_roots=("routing",),
            mcp_roots=("mcps",),
            documentation_roots=("docs",),
            source_intelligence=source_intelligence,
            test_receipt="verification/current.json",
            test_receipt_policy=receipt_policy,
            lifecycle_driver=lifecycle_driver,
            default_flavor_selectors=flavor_selectors,
            source_cache=_default_source_cache(),
            project_type=resolved_project_type,
            repository_policy=repository_policy,
        )
        active_flavors = frozenset(
            _FLAVOR_DIRECTORY_BY_CANONICAL[
                FLAVOR_SELECTOR_CANONICAL_NAMES.get(
                    selector.removeprefix("+"), selector.removeprefix("+")
                )
            ]
            for selector in flavor_selectors
        )
        _validate_template_skill_catalog(active_flavors)
        if root_plan is not None:
            from literate_ai.adapters.monorepo_adoption import MonorepoAdoptionError
            from literate_ai.adapters.monorepo_components import (
                check_monorepo_retained_receipts,
                run_component_retained_harness,
                stage_monorepo_components,
            )

            refinement = (convert_readiness or {}).get("root_refinement")
            if not isinstance(refinement, dict) or not isinstance(
                refinement.get("plan_identity"), str
            ):
                raise ProjectInitializationError(
                    "project.convert_monorepo_plan_invalid",
                    "refined monorepo conversion omitted its reviewed plan identity",
                )
            try:
                monorepo_staging = tempfile.TemporaryDirectory(
                    prefix="litai-monorepo-components-",
                    dir=resolved_target.parent,
                )
                staging_root = Path(monorepo_staging.name)
                monorepo_bundle = staging_root / "bundle"
                monorepo_manifest = stage_monorepo_components(
                    resolved_target,
                    root_plan,
                    monorepo_bundle,
                    expected_plan_identity=str(refinement["plan_identity"]),
                    acknowledged=True,
                    default_branch=default_branch,
                )
                components = monorepo_manifest.get("components")
                if not isinstance(components, list) or not all(
                    isinstance(name, str) for name in components
                ):
                    raise ProjectInitializationError(
                        "project.convert_monorepo_bundle_invalid",
                        "refined monorepo staging omitted its Component inventory",
                    )
                receipts_root = staging_root / "receipts"
                receipts_root.mkdir()
                for name in components:
                    receipt = receipts_root / f"{name}.json"
                    run_component_retained_harness(
                        resolved_target,
                        monorepo_bundle,
                        name,
                        receipt,
                        acknowledged=True,
                        worker_id="conversion-local",
                        timeout_seconds=baseline_timeout_seconds,
                    )
                    monorepo_receipts[name] = receipt
                check_monorepo_retained_receipts(
                    resolved_target,
                    monorepo_bundle,
                    monorepo_receipts,
                    expected_bundle_identity=str(monorepo_manifest["bundle_identity"]),
                )
            except BaseException as exc:
                if monorepo_staging is not None:
                    monorepo_staging.cleanup()
                    monorepo_staging = None
                if isinstance(exc, MonorepoAdoptionError):
                    raise ProjectInitializationError(exc.code, exc.message) from exc
                raise
        if convert and not interrupted:
            # Quarantine is the first conversion mutation. Complete origin, parent,
            # lineage, inherited-catalog, binding, and local-template preflight before
            # moving operator-owned source so any failure above leaves the repository
            # byte-for-byte untouched. Resume an interrupted scaffold without
            # quarantining its partial literate-ai evidence a second time (INIT-003).
            quarantine = _quarantine_existing_tree(resolved_target)
        directories = (
            "components",
            "samples",
            "flavors",
            "skills/specification-to-source",
            "skills/source-to-specification",
            "skills/agent",
            "workflows",
            "routing",
            "mcps",
            "docs",
        )
        resolved_target.mkdir(parents=True, exist_ok=True)
        _mkdir_exist_ok = convert
        for relative in directories:
            resolved_target.joinpath(*Path(relative).parts).mkdir(
                parents=True, exist_ok=_mkdir_exist_ok
            )
        (resolved_target / "verification").mkdir(exist_ok=_mkdir_exist_ok)
        # Place .gitkeep in declared roots that may start empty so Git
        # preserves them across clone.  Only target roots that a converted
        # project declares but the init template may not populate. Components
        # can also be empty when the initialized example lives under samples/.
        # docs/, flavors/, skills/, workflows/, and routing/ receive content.
        # See #207 and SUBMODULE-ORCH-001's clean-clone acceptance.
        _GITKEEP_CANDIDATES = ("components", "samples", "mcps", "verification")
        for relative in _GITKEEP_CANDIDATES:
            path = resolved_target.joinpath(*Path(relative).parts)
            if path.is_dir() and not any(path.iterdir()):
                (path / ".gitkeep").write_text("", encoding="utf-8")
        harness_inventory_document: dict[str, object] | None = None
        harness_baseline_document: dict[str, object] | None = None
        harness_parity_document: dict[str, object] | None = None
        harness_lift_shift_document: dict[str, object] | None = None
        native_rewrite_program: dict[str, object] | None = None
        shim_authority_paths: list[str] = []
        harness_wrapper_path: str | None = None
        if convert and quarantine is not None:
            # Inspect the quarantined legacy tree and generate the top-level
            # literate-ai wrapper before any other scaffold evidence is recorded.
            from literate_ai.adapters.harness_inventory import (
                HARNESS_WRAPPER_FILENAME,
                HarnessBaselineError,
                copy_retained_source_tree,
                execute_harness_baseline,
                execute_harness_wrapper_parity,
                inspect_harness,
                legacy_shim_authority,
                lift_shift_legacy_project,
                prepare_native_rewrite_program,
                render_harness_wrapper,
                stages_requiring_execution,
            )

            harness_inventory_document = inspect_harness(
                resolved_target / str(quarantine["directory"])
            )
            if harness_workspace_links:
                harness_inventory_document["workspace_links"] = (
                    harness_workspace_link_evidence(harness_workspace_links)
                )
            from literate_ai.adapters.retained_harness_receipts import (
                retained_harness_receipt_policy,
            )

            if allow_unready:
                harness_inventory_document["catalog_gaps"] = convert_readiness.get(
                    "catalog_gaps", []
                )
                harness_inventory_document["allow_unready"] = True
            definition = replace(
                definition,
                test_receipt_policy=retained_harness_receipt_policy(
                    harness_inventory_document
                ),
            )
            evidence_dir = resolved_target / ".literate"
            evidence_dir.mkdir(exist_ok=True)
            inventory_path = evidence_dir / "harness-inventory.json"
            inventory_path.write_bytes(
                canonical_json_bytes(harness_inventory_document) + b"\n"
            )
            try:
                to_run = stages_requiring_execution(
                    harness_inventory_document, run_baseline=run_baseline
                )
                if to_run:
                    # Baseline commands may create build products or otherwise mutate
                    # their workspace. Run them against an exact disposable copy so
                    # the quarantined original remains pristine. Keep that copy below
                    # the project root so nested container runtimes which only share
                    # the CI workspace can mount it.
                    with tempfile.TemporaryDirectory(
                        prefix=".literate-ai-convert-baseline-",
                        dir=resolved_target,
                    ) as baseline_directory:
                        workspace_root = Path(baseline_directory)
                        baseline_root = workspace_root / "legacy"
                        copy_retained_source_tree(
                            resolved_target / str(quarantine["directory"]),
                            baseline_root,
                            harness_inventory_document["source_scope"],
                        )
                        with materialize_harness_workspace_links(
                            workspace_root, harness_workspace_links
                        ):
                            harness_baseline_document = execute_harness_baseline(
                                harness_inventory_document,
                                legacy_root=baseline_root,
                                timeout_seconds=baseline_timeout_seconds,
                                diagnostic_limit_chars=baseline_diagnostic_chars,
                                run_baseline=run_baseline,
                            )
                else:
                    harness_baseline_document = execute_harness_baseline(
                        harness_inventory_document,
                        legacy_root=resolved_target / str(quarantine["directory"]),
                        timeout_seconds=baseline_timeout_seconds,
                        diagnostic_limit_chars=baseline_diagnostic_chars,
                        run_baseline=run_baseline,
                    )
                baseline_path = evidence_dir / "legacy-harness-baseline.json"
                baseline_path.write_bytes(
                    canonical_json_bytes(harness_baseline_document) + b"\n"
                )
                wrapper_text = render_harness_wrapper(
                    harness_inventory_document,
                    legacy_directory=str(quarantine["directory"]),
                    legacy_root=resolved_target / str(quarantine["directory"]),
                )
                wrapper_path = resolved_target / HARNESS_WRAPPER_FILENAME
                wrapper_path.write_text(wrapper_text, encoding="utf-8", newline="\n")
                harness_wrapper_path = HARNESS_WRAPPER_FILENAME
                shim_authority = legacy_shim_authority(
                    harness_inventory_document, harness_baseline_document
                )
                for relative, content in shim_authority.items():
                    path = resolved_target.joinpath(*Path(relative).parts)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8", newline="\n")
                    shim_authority_paths.append(relative)
                harness_parity_document = execute_harness_wrapper_parity(
                    harness_inventory_document,
                    harness_baseline_document,
                    project_root=resolved_target,
                    legacy_root=resolved_target / str(quarantine["directory"]),
                    timeout_seconds=baseline_timeout_seconds,
                    diagnostic_limit_chars=baseline_diagnostic_chars,
                    workspace_links=harness_workspace_links,
                )
                parity_path = evidence_dir / "legacy-wrapper-parity.json"
                parity_path.write_bytes(
                    canonical_json_bytes(harness_parity_document) + b"\n"
                )
            except (HarnessBaselineError, HarnessWorkspaceError) as exc:
                # Preserve digest-only failure evidence in the raised error, but leave
                # no Literate AI files behind: conversion is transactional at this
                # gate and returns the project to its exact pre-conversion layout.
                try:
                    _rollback_converted_tree(resolved_target, quarantine)
                except ProjectInitializationError as rollback_error:
                    raise ProjectInitializationError(
                        "project.convert_rollback_failed",
                        f"{exc.message}; rollback also failed: {rollback_error}",
                    ) from rollback_error
                raise ProjectInitializationError(
                    exc.code,
                    f"{exc.message}; literate-ai conversion was rolled back",
                ) from exc
        ProjectConfigurationStore(resolved_target).create(definition)
        created: list[str] = [PROJECT_FILENAME]
        if harness_wrapper_path is not None:
            created.extend(
                (
                    ".literate/harness-inventory.json",
                    ".literate/legacy-harness-baseline.json",
                    ".literate/legacy-wrapper-parity.json",
                    harness_wrapper_path,
                )
            )
            created.extend(shim_authority_paths)
        for destination, resource in _TEMPLATE_FILES.items():
            if not _include_template_file(destination, active_flavors):
                continue
            path = resolved_target.joinpath(*Path(destination).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_template_text(resource), encoding="utf-8", newline="\n")
            created.append(destination)
        if (
            convert
            and quarantine is not None
            and harness_inventory_document is not None
            and harness_baseline_document is not None
            and harness_parity_document is not None
        ):
            try:
                harness_lift_shift_document = lift_shift_legacy_project(
                    harness_inventory_document,
                    harness_baseline_document,
                    harness_parity_document,
                    project_root=resolved_target,
                    quarantine=quarantine,
                    timeout_seconds=baseline_timeout_seconds,
                    diagnostic_limit_chars=baseline_diagnostic_chars,
                    workspace_links=harness_workspace_links,
                )
            except (HarnessBaselineError, HarnessWorkspaceError) as exc:
                raise ProjectInitializationError(exc.code, exc.message) from exc
            lift_shift_path = evidence_dir / "legacy-lift-shift.json"
            lift_shift_path.write_bytes(
                canonical_json_bytes(harness_lift_shift_document) + b"\n"
            )
            adr_path = str(harness_lift_shift_document["adr"])
            created.extend((".literate/legacy-lift-shift.json", adr_path))
            native_rewrite_program = prepare_native_rewrite_program(
                harness_lift_shift_document, project_root=resolved_target
            )
            created.extend(
                (
                    str(native_rewrite_program["adr"]),
                    str(native_rewrite_program["roadmap"]),
                )
            )
            if (
                monorepo_bundle is not None
                and monorepo_manifest is not None
                and monorepo_staging is not None
            ):
                from literate_ai.adapters.monorepo_adoption import (
                    MonorepoAdoptionError,
                )
                from literate_ai.adapters.monorepo_components import (
                    install_monorepo_components,
                )

                retained_root = resolved_target.joinpath(
                    *Path(
                        str(harness_lift_shift_document["implementation_directory"])
                    ).parts
                )
                try:
                    monorepo_components_document = install_monorepo_components(
                        retained_root,
                        monorepo_bundle,
                        resolved_target,
                        monorepo_receipts,
                        expected_bundle_identity=str(
                            monorepo_manifest["bundle_identity"]
                        ),
                    )
                except MonorepoAdoptionError as exc:
                    try:
                        _restore_lift_shift_quarantine(
                            resolved_target, quarantine, harness_lift_shift_document
                        )
                        _rollback_converted_tree(resolved_target, quarantine)
                    except ProjectInitializationError as rollback_error:
                        raise ProjectInitializationError(
                            "project.convert_rollback_failed",
                            f"{exc.message}; rollback also failed: {rollback_error}",
                        ) from rollback_error
                    raise ProjectInitializationError(
                        exc.code,
                        f"{exc.message}; literate-ai conversion was rolled back",
                    ) from exc
                finally:
                    monorepo_staging.cleanup()
                    monorepo_staging = None
                for path in resolved_target.joinpath(
                    *Path(".literate/monorepo-components").parts
                ).rglob("*"):
                    if path.is_file():
                        created.append(path.relative_to(resolved_target).as_posix())
                for name in monorepo_components_document["components"]:
                    created.extend(
                        (
                            f"components/{name}/component.md",
                            f"components/{name}/binding.json",
                        )
                    )
        # In convert mode the project already has its own structure; never add the
        # starter hello Component regardless of the caller-supplied empty flag.
        effective_empty = empty or convert
        inherited_starter = any(
            item.kind == "component" and item.name == "hello-component"
            for item in inherited_catalogs.items
        )
        include_starter = (
            not effective_empty and resolved_project_type in PROJECT_TYPES_WITH_STARTER
        )
        if include_starter and not inherited_starter:
            for destination, resource in _STARTER_TEMPLATE_FILES.items():
                path = resolved_target.joinpath(*Path(destination).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    _template_text(resource), encoding="utf-8", newline="\n"
                )
                created.append(destination)
        if repository_lineage.nodes:
            try:
                created.extend(
                    materialize_inherited_catalogs(resolved_target, inherited_catalogs)
                )
            except RepositoryCatalogError as exc:
                raise ProjectInitializationError(exc.code, exc.message) from exc
        # Component-lock identities bind the effective pre-lock project authority
        # graph.  Persist the already-resolved lineage before creating any initial
        # locks so graph projection observes the same authority that initialization
        # resolved in memory.  This also keeps inherited catalog provenance and
        # lineage available as one coherent input boundary.
        try:
            FilesystemRepositoryLineageStore(resolved_target).replace(
                selection, repository_lineage
            )
        except RepositoryLineageStoreError as exc:
            raise ProjectInitializationError(exc.code, exc.message) from exc
        created.extend((REPOSITORY_PARENT_FILE, REPOSITORY_LINEAGE_FILE))
        # The pre-lock authority graph classifies packaged scaffold/catalog files from
        # their initialization baseline.  Record the origin and provisional baseline
        # before lock planning so initial locks bind the same ownership graph that a
        # fresh project's later `lock --check` observes.  Derived lock/audit/oracle
        # evidence is intentionally added after this point and remains dynamic rather
        # than framework-template update authority.
        evidence_root = resolved_target / ".literate"
        evidence_root.mkdir(exist_ok=True)
        origin_path = resolved_target.joinpath(*Path(INITIALIZATION_ORIGIN_FILE).parts)
        origin_path.write_bytes(canonical_json_bytes(origin.to_dict()) + b"\n")
        created.append(INITIALIZATION_ORIGIN_FILE)
        baseline = _initialization_baseline(resolved_target, origin, created)
        baseline_path = resolved_target.joinpath(
            *Path(INITIALIZATION_BASELINE_FILE).parts
        )
        baseline_path.write_bytes(canonical_json_bytes(baseline.to_dict()) + b"\n")
        if harness_inventory_document is not None:
            try:
                update_component_lock(
                    resolved_target / "components" / "legacy-project-wrapper",
                    target_name="host",
                    flavor_selectors=tuple(
                        selector
                        for selector in definition.default_flavor_selectors
                        if "/build-" in selector
                    ),
                    flavor_roots=(resolved_target / "flavors",),
                )
            except (
                ComponentLockPlanningError,
                ComponentLockResolutionError,
                ComponentLockStoreError,
                ComponentResolutionAuditStoreError,
            ) as exc:
                raise ProjectInitializationError(
                    exc.code,
                    f"legacy wrapper Component lock could not be created: {exc}",
                ) from exc
            created.extend(
                (
                    "components/legacy-project-wrapper/component.lock.json",
                    "components/legacy-project-wrapper/"
                    "component.resolution-audit.host.json",
                )
            )
        if monorepo_components_document is not None:
            for name in monorepo_components_document["components"]:
                try:
                    update_component_lock(
                        resolved_target / "components" / name,
                        target_name="host",
                        flavor_selectors=tuple(
                            selector
                            for selector in definition.default_flavor_selectors
                            if "/build-" in selector
                        ),
                        flavor_roots=(resolved_target / "flavors",),
                    )
                except (
                    ComponentLockPlanningError,
                    ComponentLockResolutionError,
                    ComponentLockStoreError,
                    ComponentResolutionAuditStoreError,
                ) as exc:
                    raise ProjectInitializationError(
                        exc.code,
                        f"refined Component {name!r} lock could not be created: {exc}",
                    ) from exc
                created.extend(
                    (
                        f"components/{name}/component.lock.json",
                        f"components/{name}/component.resolution-audit.host.json",
                    )
                )
        if (
            _include_template_file(
                "components/document-pair/component.md", active_flavors
            )
            and "components/document-pair/component.md" in created
        ):
            try:
                update_component_lock(
                    resolved_target / "components" / "document-pair",
                    target_name="host",
                    flavor_selectors=(),
                    flavor_roots=(resolved_target / "flavors",),
                )
            except (
                ComponentLockPlanningError,
                ComponentLockResolutionError,
                ComponentLockStoreError,
                ComponentResolutionAuditStoreError,
            ) as exc:
                raise ProjectInitializationError(
                    exc.code,
                    f"document-pair Component lock could not be created: {exc}",
                ) from exc
            created.extend(
                (
                    "components/document-pair/component.lock.json",
                    "components/document-pair/component.resolution-audit.host.json",
                )
            )
        if include_starter or (not effective_empty and inherited_starter):
            try:
                initial_lock = update_component_lock(
                    resolved_target / "samples" / "hello-component",
                    target_name="host",
                    flavor_selectors=(),
                    flavor_roots=(resolved_target / "flavors",),
                )
            except (
                ComponentLockPlanningError,
                ComponentLockResolutionError,
                ComponentLockStoreError,
                ComponentResolutionAuditStoreError,
            ) as exc:
                raise ProjectInitializationError(
                    exc.code,
                    f"starter Component lock could not be created: {exc}",
                ) from exc
            created.extend(
                (
                    "samples/hello-component/component.lock.json",
                    "samples/hello-component/component.resolution-audit.host.json",
                )
            )
            created.append(_write_starter_acceptance_oracle(resolved_target))
        else:
            initial_lock = None
        for destination in _STARTER_DOCS:
            path = resolved_target.joinpath(*Path(destination).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                render_starter_document(
                    destination,
                    converted=harness_lift_shift_document is not None,
                ),
                encoding="utf-8",
                newline="\n",
            )
            created.append(destination)
        try:
            initialized_project = self._validation.validate(
                resolved_target,
                require_authority_review=False,
            )
        except ProjectValidationError as exc:
            _initialization_error(exc)
        review = initialized_project["authority_review"]
        if not isinstance(review, dict):
            raise ProjectInitializationError(
                "project.init_validation_invalid",
                "project validation omitted its authority review",
            )
        current_review = self._record_authority_review(
            resolved_target,
            review,
            document=DEFAULT_AUTHORITY_REVIEW_DOCUMENT,
        )
        authority_identity = current_review.get("authority_identity")
        if not isinstance(authority_identity, str):
            raise ProjectInitializationError(
                "project.init_validation_invalid",
                "project validation omitted its complete authority identity",
            )
        # Finalize the full framework-owned baseline after generated lock evidence and
        # documentation review exist.  Its catalog sentinel set is identical to the
        # provisional pre-lock baseline above, so this enrichment cannot change the
        # authority graph to which those locks were bound.
        baseline = _initialization_baseline(resolved_target, origin, created)
        baseline_path.write_bytes(canonical_json_bytes(baseline.to_dict()) + b"\n")
        created.append(INITIALIZATION_BASELINE_FILE)
        from literate_ai.contracts import CatalogImportsFile

        from .update_merge import CHECKPOINT, UpdateBases

        bases = UpdateBases(resolved_target)
        for relative in created:
            if not relative.startswith(".literate/"):
                path = resolved_target / relative
                if path.is_file():
                    bases.retain(path.read_bytes())
        if (resolved_target / CatalogImportsFile.PATH).is_file():
            bases.remember_catalog(CatalogImportsFile.load(resolved_target).imports)
        bases.write()
        created.append(CHECKPOINT)
        try:
            finalized_project = self._validation.validate(
                resolved_target,
                require_authority_review=True,
                include_test_receipt=False,
                synchronize_source_intelligence=False,
                source_intelligence_stage=SourceIntelligenceStage.STRUCTURAL_REVIEW,
            )
        except ProjectValidationError as exc:
            _initialization_error(exc)
        finalized_review = finalized_project.get("authority_review")
        if (
            not isinstance(finalized_review, dict)
            or finalized_review.get("state") != "current"
            or finalized_review.get("authority_identity") != authority_identity
        ):
            raise ProjectInitializationError(
                "project.init_baseline_validation_failed",
                "initialization evidence changed or invalidated project authority",
            )
        try:
            private_paths = resolve_user_paths()
            test_configuration = private_paths.project_test_config(
                definition.project_id
            )
        except UserPathError as exc:
            _initialization_error(exc)
        conversion_authority = None
        if convert:
            from literate_ai.adapters.conversion_authority import (
                CONVERSION_AUTHORITY_FILE,
                ConversionAuthorityError,
                FilesystemConversionAuthorityStore,
            )

            try:
                conversion_authority = FilesystemConversionAuthorityStore(
                    resolved_target
                ).initialize(
                    project_id=definition.project_id,
                    evidence_identity=ContentIdentity.parse_uri(authority_identity),
                )
            except (ConversionAuthorityError, ValueError) as exc:
                raise ProjectInitializationError(
                    getattr(exc, "code", "conversion_authority.write_failed"),
                    getattr(exc, "message", str(exc)),
                ) from exc
            created.append(CONVERSION_AUTHORITY_FILE)
        return {
            "schema": PROJECT_INITIALIZATION_SCHEMA,
            "project_id": definition.project_id,
            "project_identity": definition.identity.uri,
            "project_authority_identity": authority_identity,
            "profile": definition.profile,
            "project_type": resolved_project_type,
            "source_intelligence": initialized_project["source_intelligence"],
            "tool_bootstrap": tool_bootstrap,
            "prerequisites": prerequisites,
            "created": sorted(set(created)),
            "initialization_origin": {
                "path": INITIALIZATION_ORIGIN_FILE,
                "identity": origin.identity.uri,
            },
            "initialization_baseline": {
                "path": INITIALIZATION_BASELINE_FILE,
                "identity": baseline.identity.uri,
                "template_protocol": baseline.template_protocol,
                "file_count": len(baseline.files),
            },
            "repository_parent": {
                "path": REPOSITORY_PARENT_FILE,
                "identity": selection.identity.uri,
            },
            "repository_lineage": {
                "path": REPOSITORY_LINEAGE_FILE,
                "identity": repository_lineage.identity.uri,
                "node_count": len(repository_lineage.nodes),
            },
            "inherited_catalogs": {
                "item_count": len(inherited_catalogs.items),
                "source_count": len(
                    {item.source.identity for item in inherited_catalogs.items}
                ),
                "provenance": (
                    ".literate/imports.json" if repository_lineage.nodes else None
                ),
            },
            "catalogs": list(directories),
            "initial_lock": initial_lock,
            "standard_binding": (
                "retained-harness"
                if harness_inventory_document is not None
                else "development-unbound"
                if standard_binding is None
                else "configured"
            ),
            "test_matrix": {
                "state": "optional-user-configured",
                "configuration": str(test_configuration),
                "example": "literate.test.example.json",
                "worker_configuration": str(private_paths.worker_config),
                "worker_example": "literate.workers.example.json",
                "documentation": "docs/user/test-matrix.md",
                "version_control": "outside-project",
            },
            "preserved": [],
            "convert_quarantine": quarantine,
            "harness_inventory": (
                {
                    "path": ".literate/harness-inventory.json",
                    "finding_count": len(harness_inventory_document["findings"]),
                    "commands": sorted(harness_inventory_document["commands"]),
                    "workspace_link_destinations": list(
                        (harness_inventory_document.get("workspace_links") or {}).get(
                            "destinations", []
                        )
                    ),
                }
                if harness_inventory_document is not None
                else None
            ),
            "harness_wrapper": harness_wrapper_path,
            "monorepo_components": monorepo_components_document,
            "shim_authority": sorted(shim_authority_paths),
            "harness_baseline": (
                {
                    "path": ".literate/legacy-harness-baseline.json",
                    "state": harness_baseline_document["state"],
                    "phase_count": harness_baseline_document["phase_count"],
                    "timeout_seconds": harness_baseline_document["timeout_seconds"],
                }
                if harness_baseline_document is not None
                else None
            ),
            "harness_parity": (
                {
                    "path": ".literate/legacy-wrapper-parity.json",
                    "state": harness_parity_document["state"],
                    "phase_count": harness_parity_document["phase_count"],
                    "timeout_seconds": harness_parity_document["timeout_seconds"],
                }
                if harness_parity_document is not None
                else None
            ),
            "lift_shift": (
                {
                    "path": ".literate/legacy-lift-shift.json",
                    "state": harness_lift_shift_document["state"],
                    "implementation_directory": harness_lift_shift_document[
                        "implementation_directory"
                    ],
                    "adr": harness_lift_shift_document["adr"],
                    "quarantine_removed": harness_lift_shift_document[
                        "quarantine_removed"
                    ],
                }
                if harness_lift_shift_document is not None
                else None
            ),
            "native_rewrite": native_rewrite_program,
            "detected_languages": list(convert_readiness.get("languages") or [])
            if convert_readiness is not None
            else [],
            "conversion_authority": (
                {
                    "path": CONVERSION_AUTHORITY_FILE,
                    "identity": conversion_authority.identity.uri,
                    **conversion_authority.to_dict(),
                }
                if conversion_authority is not None
                else None
            ),
        }

    def record_authority_review(
        self,
        selected: Path,
        *,
        document: str | Path | None = None,
    ) -> dict[str, object]:
        """Record the caller's explicit review of current project authority.

        Calling this method is the review acknowledgement. It replaces one existing
        stale/current marker, or one placeholder in an explicitly selected declared
        Markdown document, and then requires the complete authority graph to validate.
        """

        try:
            review = self._validation.documentation_review(Path(selected))
        except ProjectValidationError as exc:
            _initialization_error(exc)
        return self._record_authority_review(Path(selected), review, document=document)

    def _record_authority_review(
        self,
        selected: Path,
        review: dict[str, object],
        *,
        document: str | Path | None,
    ) -> dict[str, object]:
        state = review.get("state")
        expected_marker = review.get("expected_marker")
        review_document = review.get("document")
        if state == "duplicate":
            raise ProjectInitializationError(
                "project.documentation_authority_review_duplicate",
                "documentation authority review marker is duplicated",
            )
        if state not in {"missing", "stale", "current"} or not isinstance(
            expected_marker, str
        ):
            raise ProjectInitializationError(
                "project.documentation_authority_review_invalid",
                "project validation returned an invalid authority review",
            )
        try:
            project = discover_project(selected)
        except ProjectError as exc:
            _initialization_error(exc)
        if project is None:
            raise ProjectInitializationError(
                "project.not_found",
                f"no {PROJECT_FILENAME} found from {selected}",
            )
        try:
            declared = {
                path.relative_to(project.root).as_posix(): path
                for path in documentation_files(project)
                if path.suffix.casefold() == ".md"
            }
        except ProjectError as exc:
            _initialization_error(exc)
        placeholder = AUTHORITY_REVIEW_PLACEHOLDER.encode("utf-8")
        if document is None:
            if isinstance(review_document, str):
                selected_document = Path(review_document)
            elif state == "missing":
                placeholders: list[str] = []
                for relative_path, candidate in declared.items():
                    try:
                        body = candidate.read_bytes()
                    except OSError as exc:
                        raise ProjectInitializationError(
                            "project.documentation_authority_review_write_failed",
                            "authority review document is unreadable",
                        ) from exc
                    if body.count(placeholder) == 1 and not tuple(
                        AUTHORITY_REVIEW_MARKER.finditer(body)
                    ):
                        placeholders.append(relative_path)
                if len(placeholders) != 1:
                    raise ProjectInitializationError(
                        "project.documentation_authority_review_document_required",
                        "a declared Markdown review document is required when no "
                        "marker exists",
                    )
                selected_document = Path(placeholders[0])
            else:
                raise ProjectInitializationError(
                    "project.documentation_authority_review_document_required",
                    "a declared Markdown review document is required when no marker "
                    "exists",
                )
        else:
            selected_document = Path(document)
            if isinstance(review_document, str) and (
                selected_document.as_posix() != review_document
            ):
                raise ProjectInitializationError(
                    "project.documentation_authority_review_document_mismatch",
                    "the selected review document does not contain the sole marker",
                )
        if (
            selected_document.is_absolute()
            or not selected_document.parts
            or ".." in selected_document.parts
        ):
            raise ProjectInitializationError(
                "project.documentation_authority_review_document_invalid",
                "authority review document must be a normalized project-relative path",
            )
        relative = selected_document.as_posix()
        path = declared.get(relative)
        if path is None:
            raise ProjectInitializationError(
                "project.documentation_authority_review_document_undeclared",
                "authority review marker must be recorded in declared Markdown",
            )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ProjectInitializationError(
                "project.documentation_authority_review_write_failed",
                "authority review document is unreadable",
            ) from exc
        marker_matches = tuple(AUTHORITY_REVIEW_MARKER.finditer(content))
        placeholder_count = content.count(placeholder)
        if state == "missing":
            if marker_matches or placeholder_count != 1:
                raise ProjectInitializationError(
                    "project.documentation_authority_review_marker_missing",
                    "selected review document must contain exactly one pending marker",
                )
            updated = content.replace(placeholder, expected_marker.encode("utf-8"), 1)
        else:
            if (
                len(marker_matches) != 1
                or placeholder_count
                or review_document != relative
            ):
                raise ProjectInitializationError(
                    "project.documentation_authority_review_marker_invalid",
                    "selected review document must contain the sole authority marker",
                )
            updated = AUTHORITY_REVIEW_MARKER.sub(
                expected_marker.encode("utf-8"), content, count=1
            )
        if updated != content:
            try:
                if path.is_symlink() or not path.is_file():
                    raise OSError("review document changed before replacement")
                _atomic_replace(path, updated)
            except OSError as exc:
                raise ProjectInitializationError(
                    "project.documentation_authority_review_write_failed",
                    "authority review marker could not be recorded",
                ) from exc
        try:
            validated = self._validation.validate(
                project.root,
                require_authority_review=True,
                include_test_receipt=False,
                synchronize_source_intelligence=False,
                source_intelligence_stage=SourceIntelligenceStage.STRUCTURAL_REVIEW,
            )
        except ProjectValidationError as exc:
            _initialization_error(exc)
        current = validated["authority_review"]
        if not isinstance(current, dict) or current.get("state") != "current":
            raise ProjectInitializationError(
                "project.documentation_authority_review_invalid",
                "recorded authority review did not validate as current",
            )
        return {**current, "recorded": updated != content}


def initialize_project(
    target: Path,
    *,
    flavor_selectors: tuple[str, ...] | None = None,
    project_id: str | None = None,
    profile: str = "canonical",
    source_intelligence_provider: str = "none",
    empty: bool = False,
    project_type: str | None = None,
    bootstrap_tools: bool = True,
    convert: bool = False,
    run_baseline: bool = False,
    baseline_timeout_seconds: int = HARNESS_COMMAND_TIMEOUT_SECONDS,
    baseline_diagnostic_chars: int = HARNESS_DIAGNOSTIC_CHARS,
    allow_unready: bool = False,
    parent_selection: RepositoryParentSelection | None = None,
) -> dict[str, Any]:
    """Create one canonical project through the public filesystem adapter."""

    return FilesystemProjectInitializationAdapter().initialize(
        target,
        project_id=project_id,
        profile=profile,
        source_intelligence_provider=source_intelligence_provider,
        empty=empty,
        project_type=project_type,
        bootstrap_tools=bootstrap_tools,
        flavor_selectors=flavor_selectors,
        convert=convert,
        run_baseline=run_baseline,
        baseline_timeout_seconds=baseline_timeout_seconds,
        baseline_diagnostic_chars=baseline_diagnostic_chars,
        allow_unready=allow_unready,
        parent_selection=parent_selection,
    )


def record_project_authority_review(
    selected: Path,
    *,
    document: str | Path | None = None,
) -> dict[str, object]:
    """Record an explicit authority review after a caller layers project catalogs."""

    return FilesystemProjectInitializationAdapter().record_authority_review(
        selected, document=document
    )


__all__ = [
    "DEFAULT_AUTHORITY_REVIEW_DOCUMENT",
    "FilesystemProjectInitializationAdapter",
    "INITIALIZATION_BASELINE_FILE",
    "INITIALIZATION_ORIGIN_FILE",
    "KNOWN_FLAVOR_SELECTORS",
    "canonical_flavor_catalog_collisions",
    "canonical_flavor_coordinate",
    "canonical_flavor_selector",
    "flavor_selector_directory",
    "PROJECT_INITIALIZATION_SCHEMA",
    "PROJECT_INITIALIZATION_PREREQUISITES_SCHEMA",
    "ProjectInitializationError",
    "_FLAVOR_AXIS_DEFAULTS",
    "FLAVOR_SELECTOR_CANONICAL_NAMES",
    "detect_repo_flavors",
    "detected_language_flavors",
    "apply_init_flavor_defaults",
    "plan_convert",
    "CONVERT_PLAN_SCHEMA",
    "discover_installed_initialization_origin",
    "host_platform_selector",
    "initialize_project",
    "record_project_authority_review",
]
