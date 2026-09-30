"""Explicit, non-destructive migration from welded Component JSON to component.md."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    path_is_link_or_reparse,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.component_markdown import (
    ComponentMarkdownError,
    parse_component_markdown,
    render_component_markdown,
)
from literate_ai.application.component_authoring_migration import (
    ComponentAuthoringMigrationError,
    LoadedRepositoryDependency,
    migrate_component_authoring,
)
from literate_ai.compatibility.component_authoring import (
    legacy_component_compatibility_exit,
)
from literate_ai.contracts import ComponentDefinition, ContentIdentity, ContentReference
from literate_ai.contracts.component_locking import (
    ComponentAuthoring,
    ComponentContentSelector,
)
from literate_ai.contracts.repositories import RepositorySourceDependency
from literate_ai.projects import (
    PROJECT_FILENAME,
    PinnedInputClosure,
    PinnedInputClosureError,
    discover_project,
    project_boundary,
)

from .errors import CliFailure

_LEGACY_FILENAME = "component.json"
_AUTHORING_FILENAME = "component.md"
_MIGRATION_LOCK = ".component.migrate.write.lock"
_MIGRATION_SET_LOCK = ".component.migration-set.write.lock"
_MAXIMUM_EXISTING_AUTHORING_BYTES = 2 * 1024 * 1024
_MAXIMUM_PROJECT_COMPONENTS = 4_096


def component_authoring_from_args(args) -> tuple[dict[str, object], int]:
    """Migrate one Component or one complete canonical project transaction."""

    try:
        selected = Path(args.component).resolve(strict=True)
        roots, operation_root, project_mode = _migration_roots(selected)
    except CliFailure:
        raise
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise CliFailure(
            "component_migration.component_invalid",
            "migration target is unavailable or has an unsafe path",
        ) from exc
    if not project_mode:
        if args.check or args.diff:
            return _one_component_authoring(args)
        try:
            with exclusive_cache_lock(operation_root / _MIGRATION_SET_LOCK):
                return _one_component_authoring(args)
        except CacheLockError as exc:
            raise CliFailure("component_migration.lock_failed", str(exc)) from exc

    check_args = _migration_args(args, check=True, difference=False)
    reports = [_one_component_authoring(check_args, component=root) for root in roots]
    if args.check or args.diff:
        return _project_report(operation_root, args, reports), max(
            (status for _report, status in reports), default=0
        )
    if any(report["state"] == "conflict" for report, _status in reports):
        return _project_report(operation_root, args, reports), 1

    created: list[tuple[Path, str]] = []
    try:
        with exclusive_cache_lock(operation_root / _MIGRATION_SET_LOCK):
            preflight = [
                _one_component_authoring(check_args, component=root) for root in roots
            ]
            if any(report["state"] == "conflict" for report, _status in preflight):
                return _project_report(operation_root, args, preflight), 1
            updated: list[tuple[dict[str, object], int]] = []
            for root, (report, _status) in zip(roots, preflight, strict=True):
                if report["state"] == "current":
                    updated.append((report, 0))
                    continue
                result, status = _one_component_authoring(args, component=root)
                updated.append((result, status))
                if result.get("state") == "created":
                    identity = result.get("rendered_content_identity")
                    if not isinstance(identity, str):
                        raise ComponentAuthoringMigrationError(
                            "publication-invalid",
                            "migration result omitted its rendered content identity",
                        )
                    created.append((root / _AUTHORING_FILENAME, identity))
            final = [
                _one_component_authoring(check_args, component=root) for root in roots
            ]
            if any(status != 0 for _report, status in final):
                raise ComponentAuthoringMigrationError(
                    "project-finalization-failed",
                    "project migration did not finalize every Component",
                )
            final_by_root = {Path(item["component"]): item for item, _status in final}
            for report, _status in updated:
                if report.get("state") == "created":
                    final_by_root[Path(str(report["component"]))] = report
            ordered = [(final_by_root[root], 0) for root in roots]
            return _project_report(operation_root, args, ordered), 0
    except Exception:
        for path, identity in reversed(created):
            _remove_exact_created(path, identity)
        raise


def _one_component_authoring(
    args,
    *,
    component: Path | None = None,
) -> tuple[dict[str, object], int]:
    """Migrate one legacy Component without rewriting its source manifest."""

    try:
        root = _component_root(Path(args.component) if component is None else component)
        boundary = project_boundary(root, legacy=root.parent)
        closure = PinnedInputClosure()
        legacy_path = root / _LEGACY_FILENAME
        legacy_bytes = closure.pin(
            legacy_path,
            boundary=boundary,
            label=f"component-migration:{root.name}:legacy-definition",
        )
        definition = _legacy_definition(legacy_bytes)
        definition = _normalize_legacy_definition(
            definition,
            root=root,
            boundary=boundary,
            closure=closure,
        )
        output = root / _AUTHORING_FILENAME
        capability_interfaces = _existing_capability_interfaces(
            output,
            definition=definition,
            root=root,
            boundary=boundary,
            closure=closure,
        )

        def load_repository_dependency(
            reference: ContentReference,
        ) -> LoadedRepositoryDependency:
            dependency_bytes = closure.pin(
                root.joinpath(*Path(reference.uri).parts),
                boundary=boundary,
                label=(
                    f"component-migration:{root.name}:repository:"
                    f"{reference.identity.digest}"
                ),
                expected_identity=reference.identity,
            )
            try:
                dependency = RepositorySourceDependency.from_dict(
                    json.loads(dependency_bytes)
                )
            except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ComponentAuthoringMigrationError(
                    "repository-dependency-invalid",
                    f"welded repository dependency {reference.uri!r} is invalid",
                ) from exc
            if dependency.integration_contract is not None:
                dependency = replace(
                    dependency,
                    integration_contract=_normalized_reference(
                        dependency.integration_contract,
                        root=root,
                        boundary=boundary,
                        closure=closure,
                        label=(f"repository:{dependency.dependency_id}:integration"),
                        project_relative=False,
                    ),
                )
            return LoadedRepositoryDependency(
                dependency,
                ContentIdentity.parse_uri(
                    "sha256:" + hashlib.sha256(dependency_bytes).hexdigest()
                ),
            )

        authoring = migrate_component_authoring(
            definition,
            load_repository_dependency,
            capability_interfaces=capability_interfaces,
        )
        rendered = render_component_markdown(
            authoring,
            output,
            project_root=boundary,
        )
        rendered_bytes = rendered.encode("utf-8")
        reparsed = parse_component_markdown(
            output,
            rendered,
            project_root=boundary,
        )
        if reparsed != authoring:
            raise ComponentAuthoringMigrationError(
                "render-roundtrip-mismatch",
                "rendered component.md does not preserve the migrated authoring model",
            )

        if args.check or args.diff:
            current = _read_existing(output)
            closure.require_unchanged()
            if _read_existing(output) != current:
                raise ComponentAuthoringMigrationError(
                    "authoring-concurrent-change",
                    "component.md changed during migration check",
                )
            return _assess_existing(
                root,
                output,
                current,
                authoring=authoring,
                rendered=rendered_bytes,
                project_root=boundary,
            )

        with exclusive_cache_lock(root / _MIGRATION_LOCK):
            require_safe_directory(root)
            current = _read_existing(output)
            closure.require_unchanged()
            if current is not None:
                return _assess_existing(
                    root,
                    output,
                    current,
                    authoring=authoring,
                    rendered=rendered_bytes,
                    project_root=boundary,
                )
            _publish_new(output, rendered_bytes, closure=closure, root=root)
        return _report(
            root,
            authoring.identity.uri,
            rendered_bytes,
            state="created",
            updated=True,
        ), 0
    except CliFailure:
        raise
    except (
        CacheLockError,
        ComponentAuthoringMigrationError,
        ComponentMarkdownError,
        PinnedInputClosureError,
        UnsafeFilesystemPathError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        code = str(getattr(exc, "code", "failed"))
        if not code.startswith("component_migration."):
            code = f"component_migration.{code}"
        raise CliFailure(code, str(exc)) from exc


def _existing_capability_interfaces(
    output: Path,
    *,
    definition: ComponentDefinition,
    root: Path,
    boundary: Path,
    closure: PinnedInputClosure,
) -> dict[str, ComponentContentSelector]:
    """Resolve human-authored interface selectors into legacy contract identities."""

    if not any(item.contract is not None for item in definition.provides):
        return {}
    current = _read_existing(output)
    if current is None:
        return {}
    authored = parse_component_markdown(
        output,
        current.decode("utf-8"),
        project_root=boundary,
    )
    legacy = {item.name: item for item in definition.provides}
    interfaces: dict[str, ComponentContentSelector] = {}
    for provided in authored.provides:
        interface = provided.interface
        if interface is None:
            continue
        capability = legacy.get(provided.name)
        if (
            capability is None
            or capability.version != provided.version
            or provided.name in interfaces
        ):
            raise ComponentAuthoringMigrationError(
                "capability-interface-ambiguous",
                f"authored interface for capability {provided.name!r} does not "
                "identify one exact legacy capability",
            )
        content = closure.pin(
            root.joinpath(*Path(interface.uri).parts),
            boundary=boundary,
            label=f"component-migration:{root.name}:interface:{provided.name}",
            expected_identity=interface.pin,
        )
        observed = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(content).hexdigest()
        )
        if capability.contract != observed:
            raise ComponentAuthoringMigrationError(
                "capability-interface-content-identity-mismatch",
                f"authored interface for capability {provided.name!r} does not "
                "match the welded contract identity",
            )
        interfaces[provided.name] = interface
    return interfaces


def _migration_roots(selected: Path) -> tuple[tuple[Path, ...], Path, bool]:
    if (selected / _LEGACY_FILENAME).is_file():
        root = _component_root(selected)
        project = discover_project(root)
        return (root,), (root if project is None else project.root), False
    project = discover_project(selected)
    if (
        project is None
        or selected != project.root
        or not (selected / PROJECT_FILENAME).is_file()
    ):
        raise CliFailure(
            "component_migration.component_invalid",
            "migration target must be a Component directory or canonical project root",
        )
    roots: list[Path] = []
    entries_seen = 0
    for catalog in project.roots("component"):
        require_safe_directory(catalog)
        pending = [catalog]
        while pending:
            directory = pending.pop()
            require_safe_directory(directory)
            children: list[tuple[Path, os.stat_result]] = []
            try:
                with os.scandir(directory) as stream:
                    for entry in stream:
                        entries_seen += 1
                        if entries_seen > _MAXIMUM_PROJECT_COMPONENTS * 64:
                            raise CliFailure(
                                "component_migration.project_limit",
                                "project Component catalogs exceed the traversal limit",
                            )
                        metadata = entry.stat(follow_symlinks=False)
                        if stat_is_link_or_reparse(metadata):
                            raise CliFailure(
                                "component_migration.component_invalid",
                                "Component catalogs cannot contain links or "
                                "reparse points",
                            )
                        children.append((Path(entry.path), metadata))
            except CliFailure:
                raise
            except OSError as exc:
                raise CliFailure(
                    "component_migration.component_invalid",
                    "Component catalog changed or became unavailable during traversal",
                ) from exc
            for path, metadata in sorted(
                children, key=lambda item: item[0].name, reverse=True
            ):
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise CliFailure(
                        "component_migration.component_invalid",
                        "Component catalogs require regular files and directories",
                    )
                if path.name != _LEGACY_FILENAME:
                    continue
                manifest = path
                if len(roots) >= _MAXIMUM_PROJECT_COMPONENTS:
                    raise CliFailure(
                        "component_migration.project_limit",
                        "project contains too many legacy Components to migrate "
                        "atomically",
                    )
                if path_is_link_or_reparse(manifest) or not manifest.is_file():
                    raise CliFailure(
                        "component_migration.component_invalid",
                        "legacy Component manifests must be direct regular files",
                    )
                roots.append(_component_root(manifest.parent))
    unique = tuple(sorted(set(roots)))
    if not unique:
        raise CliFailure(
            "component_migration.legacy_missing",
            "project has no legacy component.json manifests",
        )
    return unique, project.root, True


def _migration_args(args, *, check: bool, difference: bool) -> Namespace:
    values = vars(args).copy()
    values["check"] = check
    values["diff"] = difference
    return Namespace(**values)


def _project_report(
    project: Path,
    args,
    reports: list[tuple[dict[str, object], int]],
) -> dict[str, object]:
    return {
        "schema": "literate-ai/project-component-authoring-migration@1",
        "project": str(project),
        "mode": "diff" if args.diff else "check" if args.check else "update",
        "current": all(status == 0 for _report, status in reports),
        "updated_count": sum(
            report.get("state") == "created" for report, _status in reports
        ),
        "components": [report for report, _status in reports],
        "legacy_preserved": True,
        "compatibility_exit": legacy_component_compatibility_exit(),
        "rollback": (
            "delete only component.md files reported as created; every component.json "
            "remains byte-for-byte unchanged"
        ),
    }


def _component_root(selected: Path) -> Path:
    try:
        root = selected.resolve(strict=True)
        require_safe_directory(root)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise CliFailure(
            "component_migration.component_invalid",
            "migration target must be a direct, existing Component directory",
        ) from exc
    legacy = root / _LEGACY_FILENAME
    if path_is_link_or_reparse(legacy) or not legacy.is_file():
        raise CliFailure(
            "component_migration.legacy_missing",
            "migration target must contain a regular component.json",
        )
    return root


def _legacy_definition(content: bytes) -> ComponentDefinition:
    try:
        return ComponentDefinition.from_dict(json.loads(content))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ComponentAuthoringMigrationError(
            "legacy-invalid", "legacy component.json is malformed or unsupported"
        ) from exc


def _normalize_legacy_definition(
    definition: ComponentDefinition,
    *,
    root: Path,
    boundary: Path,
    closure: PinnedInputClosure,
) -> ComponentDefinition:
    global_kinds = {
        "model-selection",
        "routing-policy",
        "specification-to-source-skill",
        "toolchain-constraint",
        "workflow",
    }
    specifications: list[str] = []
    for index, value in enumerate(definition.specification_roots):
        content = closure.pin(
            root.joinpath(*Path(value).parts),
            boundary=boundary,
            label=f"component-migration:{root.name}:specification:{index}",
        )
        del content
        specifications.append(
            _component_relative_path(root.joinpath(*Path(value).parts), root=root)
        )

    def normalized(reference: ContentReference, label: str) -> ContentReference:
        return _normalized_reference(
            reference,
            root=root,
            boundary=boundary,
            closure=closure,
            label=label,
            project_relative=reference.kind in global_kinds,
        )

    return replace(
        definition,
        specification_roots=tuple(specifications),
        authoring_inputs=tuple(
            normalized(item, f"authoring-input:{index}")
            for index, item in enumerate(definition.authoring_inputs)
        ),
        workflow_definition=_normalized_reference(
            definition.workflow_definition,
            root=root,
            boundary=boundary,
            closure=closure,
            label="workflow",
            project_relative=True,
        ),
        routing_policy=_normalized_reference(
            definition.routing_policy,
            root=root,
            boundary=boundary,
            closure=closure,
            label="routing",
            project_relative=True,
        ),
        acceptance_contracts=tuple(
            _normalized_reference(
                item,
                root=root,
                boundary=boundary,
                closure=closure,
                label=f"acceptance:{index}",
                project_relative=False,
            )
            for index, item in enumerate(definition.acceptance_contracts)
        ),
        source_dependencies=tuple(
            _normalized_reference(
                item,
                root=root,
                boundary=boundary,
                closure=closure,
                label=f"repository-dependency:{index}",
                project_relative=False,
            )
            for index, item in enumerate(definition.source_dependencies)
        ),
    )


def _normalized_reference(
    reference: ContentReference,
    *,
    root: Path,
    boundary: Path,
    closure: PinnedInputClosure,
    label: str,
    project_relative: bool,
) -> ContentReference:
    lexical = root.joinpath(*Path(reference.uri).parts)
    closure.pin(
        lexical,
        boundary=boundary,
        label=f"component-migration:{root.name}:{label}",
        expected_identity=reference.identity,
    )
    if project_relative:
        resolved = lexical.resolve(strict=True)
        try:
            normalized = resolved.relative_to(boundary.resolve(strict=True)).as_posix()
        except ValueError as exc:
            raise ComponentAuthoringMigrationError(
                "selector-path-escape",
                f"legacy selector {reference.uri!r} escapes the project boundary",
            ) from exc
    else:
        normalized = _component_relative_path(lexical, root=root)
    return ContentReference(reference.kind, normalized, reference.identity)


def _component_relative_path(path: Path, *, root: Path) -> str:
    try:
        return path.resolve(strict=True).relative_to(root).as_posix()
    except ValueError as exc:
        raise ComponentAuthoringMigrationError(
            "selector-path-escape",
            "legacy Component-local selector escapes the Component directory",
        ) from exc


def _read_existing(path: Path) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ComponentAuthoringMigrationError(
            "authoring-path-unsafe",
            "existing component.md must be a direct regular file",
        )
    if before.st_size > _MAXIMUM_EXISTING_AUTHORING_BYTES:
        raise ComponentAuthoringMigrationError(
            "authoring-document-limit", "existing component.md is too large"
        )
    content = path.read_bytes()
    after = path.lstat()
    if (
        stat_is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or _signature(before) != _signature(after)
    ):
        raise ComponentAuthoringMigrationError(
            "authoring-concurrent-change", "component.md changed while inspected"
        )
    return content


def _parse_existing_authoring(
    path: Path,
    content: bytes,
    *,
    project_root: Path,
):
    try:
        return parse_component_markdown(
            path,
            content.decode("utf-8"),
            project_root=project_root,
        )
    except (ComponentMarkdownError, UnicodeError, TypeError, ValueError):
        return None


def _assess_existing(
    root: Path,
    output: Path,
    current: bytes | None,
    *,
    authoring: ComponentAuthoring,
    rendered: bytes,
    project_root: Path,
) -> tuple[dict[str, object], int]:
    if current is None:
        return (
            _report(
                root,
                authoring.identity.uri,
                rendered,
                state="missing",
                updated=False,
            ),
            1,
        )
    current_authoring = _parse_existing_authoring(
        output,
        current,
        project_root=project_root,
    )
    identity = None if current_authoring is None else current_authoring.identity.uri
    if current_authoring != authoring:
        return (
            _report(
                root,
                authoring.identity.uri,
                rendered,
                state="conflict",
                updated=False,
                current=current,
                current_authoring_identity=identity,
            ),
            1,
        )
    return (
        _report(
            root,
            authoring.identity.uri,
            rendered,
            state="current",
            updated=False,
            current=current,
            current_authoring_identity=identity,
        ),
        0,
    )


def _publish_new(
    path: Path,
    content: bytes,
    *,
    closure: PinnedInputClosure,
    root: Path,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{_AUTHORING_FILENAME}.", dir=root
    )
    temporary = Path(temporary_name)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        closure.require_unchanged()
        require_safe_directory(root)
        if path_is_link_or_reparse(path) or path.exists():
            raise ComponentAuthoringMigrationError(
                "authoring-concurrent-change",
                "component.md appeared while migration was staged",
            )
        try:
            os.link(temporary, path)
            linked = True
        except FileExistsError as exc:
            raise ComponentAuthoringMigrationError(
                "authoring-concurrent-change",
                "component.md appeared while migration was staged",
            ) from exc
        _fsync_directory(root)
    except Exception:
        if linked:
            try:
                staged = temporary.lstat()
                published = path.lstat()
                if (staged.st_dev, staged.st_ino) == (
                    published.st_dev,
                    published.st_ino,
                ):
                    path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _remove_exact_created(path: Path, expected_identity: str) -> bool:
    """Remove only the exact regular file created by this transaction.

    A concurrent replacement is external state and must never be deleted while
    attempting rollback.
    """

    try:
        before = path.lstat()
        if stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            return False
        if before.st_size > _MAXIMUM_EXISTING_AUTHORING_BYTES:
            return False
        content = path.read_bytes()
        after = path.lstat()
        if _signature(before) != _signature(after):
            return False
        observed = "sha256:" + hashlib.sha256(content).hexdigest()
        if observed != expected_identity:
            return False
        path.unlink()
        _fsync_directory(path.parent)
        return True
    except (FileNotFoundError, OSError):
        return False


def _report(
    root: Path,
    authoring_identity: str,
    content: bytes,
    *,
    state: str,
    updated: bool,
    current: bytes | None = None,
    current_authoring_identity: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "literate-ai/component-authoring-migration@1",
        "component": str(root),
        "legacy_path": str(root / _LEGACY_FILENAME),
        "authoring_path": str(root / _AUTHORING_FILENAME),
        "authoring_identity": authoring_identity,
        "rendered_content_identity": ("sha256:" + hashlib.sha256(content).hexdigest()),
        "state": state,
        "updated": updated,
        "legacy_preserved": True,
        "compatibility_exit": legacy_component_compatibility_exit(),
        "rollback": (
            "delete component.md to return to the preserved component.json authority"
        ),
    }
    if current is not None:
        result["current_content_identity"] = (
            "sha256:" + hashlib.sha256(current).hexdigest()
        )
        result["current_authoring_identity"] = current_authoring_identity
        result["semantic_match"] = current_authoring_identity == authoring_identity
    return result


def _signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["component_authoring_from_args"]
