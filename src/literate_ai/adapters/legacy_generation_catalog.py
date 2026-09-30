"""Filesystem adapter for legacy unlocked Component and Flavor catalogs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.adapters.component_markdown import (
    ComponentMarkdownError,
    parse_component_markdown,
)
from literate_ai.adapters.flavor_markdown import (
    FlavorMarkdownError,
    parse_flavor_markdown,
)
from literate_ai.adapters.generation_preparation import load_flavor_contributions
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.adapters.models import (
    CODING_CLIS,
    PORTABLE_APPLICATION_SCHEMA,
    CodingCliError,
    GenerationRecipe,
    RecipeDocument,
    RecipeFlavor,
    RecipeSkill,
    validate_portable_application,
)
from literate_ai.adapters.specifications import (
    SPECIFICATION_PROVIDER_ERRORS,
    LoadedOpenSpec,
    LoadedSpecification,
    OpenSpecProvider,
    load_specification_provider,
)
from literate_ai.application.generation_preparation import GenerationPreparationError
from literate_ai.composition import ComponentComposition, FlavorResolution
from literate_ai.contracts import (
    REPOSITORY_SOURCE_DEPENDENCY_CONTENT_KIND,
    ComponentAuthoring,
    ComponentContentSelector,
    ComponentDefinition,
    ComponentRevision,
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    ContributionKind,
    ContributionReference,
    FlavorDefinition,
    FlavorRevision,
    HashAlgorithm,
    LockedComponentRevision,
    RepositorySourceDependency,
    SpecificationSet,
    TargetProfile,
    ToolchainConstraint,
    canonical_identity,
    source_cache_model_selector,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    PinnedInputClosure,
    ProjectConfigurationStore,
    ProjectError,
    discover_project,
    project_boundary,
    specification_to_source_skill_paths,
)
from literate_ai.registry import FlavorDescriptor

CliFailure = GenerationPreparationError

MODEL_SELECTION_SCHEMA = "literate-ai/coding-model-selection@1"

MAXIMUM_PINNED_INPUT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _PreparedGeneration:
    component_root: Path
    boundary: Path
    definition: ComponentDefinition
    recipe: GenerationRecipe
    selected_flavors: tuple[RecipeFlavor, ...]
    component_revision: ComponentRevision | LockedComponentRevision | None = None
    target_profile: TargetProfile | None = None
    component_composition: ComponentComposition | None = None
    flavor_resolution: FlavorResolution | None = None
    toolchain_constraints: tuple[_EffectiveToolchainConstraint, ...] = ()
    input_closure: PinnedInputClosure | None = None
    flavor_catalog_audit_identity: ContentIdentity | None = None
    locked_authority_snapshot: LockedGenerationAuthoritySnapshot | None = None


@dataclass(frozen=True, slots=True)
class ComponentAuthorityConvergence:
    """Exact dual-manifest inputs after their executable semantics converge."""

    definition: ComponentDefinition
    legacy_content: bytes
    authoring: ComponentAuthoring | None
    authoring_content: bytes | None
    semantic_identity: ContentIdentity | None


@dataclass(frozen=True, slots=True)
class _ResolvedToolchainConstraint:
    source: str
    reference: ContentReference
    constraint: ToolchainConstraint
    contribution: ContributionReference | None = None

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "source": self.source,
            "content_identity": self.reference.identity.uri,
        }
        if self.contribution is not None:
            value["contribution_id"] = self.contribution.contribution_id
            value["slot"] = self.contribution.slot
        return value


@dataclass(frozen=True, slots=True)
class _EffectiveToolchainConstraint:
    constraint: ToolchainConstraint
    sources: tuple[_ResolvedToolchainConstraint, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "constraint": self.constraint.to_dict(),
            "sources": [item.to_dict() for item in self.sources],
        }


@dataclass(frozen=True, slots=True)
class _LoadedFlavor:
    recipe: RecipeFlavor
    definition: FlavorDefinition
    specification_set: SpecificationSet
    revision: FlavorRevision
    descriptor: FlavorDescriptor
    input_closure: PinnedInputClosure
    toolchain_constraints: tuple[_ResolvedToolchainConstraint, ...] = ()


def _flavor_catalog_audit_identity(
    catalog: Sequence[_LoadedFlavor],
) -> ContentIdentity:
    """Identify the verified discovery catalog without making it derivation input."""

    return canonical_identity(
        {
            "schema": "literate-ai/flavor-catalog-audit@1",
            "flavors": [
                {
                    "coordinate": flavor.definition.coordinate.uri,
                    "revision_identity": flavor.revision.identity.uri,
                    "specification_set_identity": flavor.specification_set.identity.uri,
                    "input_closure_identity": flavor.input_closure.identity,
                }
                for flavor in sorted(
                    catalog,
                    key=lambda item: (
                        item.definition.coordinate.uri,
                        item.revision.identity.uri,
                    ),
                )
            ],
        }
    )


def _text(content: str | bytes) -> str:
    return content.decode("utf-8") if isinstance(content, bytes) else content


def _bounded_file_content(path: Path, *, code: str, label: str) -> bytes:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAXIMUM_PINNED_INPUT_BYTES + 1)
    except OSError as exc:
        raise CliFailure(code, f"{label} is unavailable") from exc
    if len(content) > MAXIMUM_PINNED_INPUT_BYTES:
        raise CliFailure(code, f"{label} exceeds the pinned-input size limit")
    return content


def _pinned_reference_content(
    component_root: Path,
    reference,
    *,
    boundary: Path,
    label: str,
) -> bytes:
    configured = component_root / reference.uri
    try:
        path = configured.resolve(strict=True)
    except OSError as exc:
        raise CliFailure(
            f"plan.{label}_unavailable", f"pinned {label} is unavailable"
        ) from exc
    if (
        configured.is_symlink()
        or not path.is_file()
        or not path.is_relative_to(boundary.resolve(strict=True))
    ):
        raise CliFailure(
            f"plan.{label}_unavailable",
            f"pinned {label} must be a regular file inside the project boundary",
        )
    content = _bounded_file_content(
        path,
        code=f"plan.{label}_unavailable",
        label=f"pinned {label}",
    )
    if hashlib.sha256(content).hexdigest() != reference.identity.digest:
        raise CliFailure(f"plan.{label}_drift", f"pinned {label} identity changed")
    return content


def _authored_flavor_reference_content(
    flavor_root: Path, uri: str, *, boundary: Path
) -> bytes:
    configured = flavor_root.joinpath(*PurePosixPath(uri).parts)
    try:
        path = configured.resolve(strict=True)
    except OSError as exc:
        raise CliFailure(
            "generate.invalid_flavor", "Flavor authoring reference is unavailable"
        ) from exc
    if (
        configured.is_symlink()
        or not path.is_file()
        or not path.is_relative_to(boundary.resolve(strict=True))
    ):
        raise CliFailure(
            "generate.invalid_flavor",
            "Flavor authoring reference must be a regular file inside the project",
        )
    return _bounded_file_content(
        path,
        code="generate.invalid_flavor",
        label="Flavor authoring reference",
    )


def _pin_project_manifest(
    closure: PinnedInputClosure,
    root: Path,
    *,
    label_prefix: str,
) -> None:
    project = discover_project(root)
    if project is None:
        return
    try:
        snapshot = ProjectConfigurationStore(project.root).read()
    except ProjectError as exc:
        raise CliFailure(
            "generate.invalid_project", "Literate AI project manifest is invalid"
        ) from exc
    if snapshot.definition != project.definition:
        raise CliFailure(
            "generate.input_capture_drift",
            "Literate AI project manifest changed while generation inputs were loaded",
        )
    closure.pin(
        snapshot.root / PROJECT_FILENAME,
        boundary=project.root,
        label=f"{label_prefix}:project-manifest",
        expected_content=snapshot.content,
    )


def _pin_loaded_specifications(
    closure: PinnedInputClosure,
    root: Path,
    loaded: LoadedOpenSpec,
    *,
    boundary: Path,
    label_prefix: str,
) -> None:
    for relative, content in loaded.contents:
        closure.pin(
            root.joinpath(*Path(relative).parts),
            boundary=boundary,
            label=f"{label_prefix}:specification:{relative}",
            expected_content=content,
        )


def _pin_definition_references(
    closure: PinnedInputClosure,
    root: Path,
    definition: ComponentDefinition | FlavorDefinition,
    *,
    boundary: Path,
    label_prefix: str,
) -> None:
    references: list[tuple[str, ContentReference]] = [
        *(
            (f"authoring-input:{index}", reference)
            for index, reference in enumerate(definition.authoring_inputs)
        )
    ]
    if isinstance(definition, ComponentDefinition):
        references.extend(
            (
                ("workflow", definition.workflow_definition),
                ("routing-policy", definition.routing_policy),
                *(
                    (f"acceptance-interface:{index}", reference)
                    for index, reference in enumerate(definition.acceptance_contracts)
                ),
                *(
                    (f"source-dependency:{index}", reference)
                    for index, reference in enumerate(definition.source_dependencies)
                ),
            )
        )
    else:
        references.extend(
            (
                f"contribution:{contribution.contribution_id}",
                contribution.content,
            )
            for contribution in definition.contributions
        )
    for role, reference in references:
        content = _pinned_reference_content(
            root,
            reference,
            boundary=boundary,
            label=role.replace("-", "_"),
        )
        closure.pin(
            root / reference.uri,
            boundary=boundary,
            label=f"{label_prefix}:{role}:{reference.kind}",
            expected_content=content,
            expected_identity=reference.identity,
        )
    repository_references = (
        definition.source_dependencies
        if isinstance(definition, ComponentDefinition)
        else tuple(
            contribution.content
            for contribution in definition.contributions
            if contribution.kind is ContributionKind.SOURCE_DEPENDENCY
        )
    )
    for index, reference in enumerate(repository_references):
        dependency = _load_repository_source_dependency(
            root,
            reference,
            boundary=boundary,
            source=f"{label_prefix}:source-dependency:{index}",
        )
        if dependency.integration_contract is None:
            continue
        content = _pinned_reference_content(
            root,
            dependency.integration_contract,
            boundary=boundary,
            label="repository_source_integration_contract",
        )
        closure.pin(
            root / dependency.integration_contract.uri,
            boundary=boundary,
            label=f"{label_prefix}:source-dependency:{index}:integration-contract",
            expected_content=content,
            expected_identity=dependency.integration_contract.identity,
        )


def _load_repository_source_dependency(
    root: Path,
    reference: ContentReference,
    *,
    boundary: Path,
    source: str,
) -> RepositorySourceDependency:
    if reference.kind != REPOSITORY_SOURCE_DEPENDENCY_CONTENT_KIND:
        raise CliFailure(
            "generate.invalid_repository_source_dependency",
            f"{source} must use a repository-source-dependency content reference",
        )
    content = _pinned_reference_content(
        root,
        reference,
        boundary=boundary,
        label="repository_source_dependency",
    )
    try:
        dependency = RepositorySourceDependency.from_dict(
            json.loads(content.decode("utf-8")), path=source
        )
    except (UnicodeError, json.JSONDecodeError, ContractValidationError) as exc:
        raise CliFailure(
            "generate.invalid_repository_source_dependency",
            f"{source} has an invalid repository source dependency",
        ) from exc
    if dependency.integration_contract is not None:
        _pinned_reference_content(
            root,
            dependency.integration_contract,
            boundary=boundary,
            label="repository_source_integration_contract",
        )
    return dependency


def _component_repository_source_dependencies(
    root: Path,
    definition: ComponentDefinition,
    *,
    boundary: Path,
) -> tuple[RepositorySourceDependency, ...]:
    dependencies = tuple(
        _load_repository_source_dependency(
            root,
            reference,
            boundary=boundary,
            source=(
                f"Component {definition.coordinate.uri} repository source "
                f"dependency {index}"
            ),
        )
        for index, reference in enumerate(definition.source_dependencies)
    )
    ids = tuple(item.dependency_id for item in dependencies)
    if len(ids) != len(set(ids)):
        raise CliFailure(
            "generate.duplicate_repository_source_dependency",
            "Component repository source dependency IDs must be unique",
        )
    return dependencies


def _component_authority_closure(
    root: Path,
    definition: ComponentDefinition,
    loaded: LoadedOpenSpec,
    manifest_content: bytes,
) -> PinnedInputClosure:
    boundary = project_boundary(root, legacy=root.parent)
    closure = PinnedInputClosure()
    prefix = f"component:{definition.coordinate.uri}"
    _pin_project_manifest(closure, root, label_prefix=prefix)
    closure.pin(
        root / "component.json",
        boundary=boundary,
        label=f"{prefix}:manifest",
        expected_content=manifest_content,
    )
    _pin_loaded_specifications(
        closure,
        root,
        loaded,
        boundary=boundary,
        label_prefix=prefix,
    )
    _pin_definition_references(
        closure,
        root,
        definition,
        boundary=boundary,
        label_prefix=prefix,
    )
    return closure


def _flavor_authority_closure(
    root: Path,
    definition_path: Path,
    definition: FlavorDefinition,
    loaded: LoadedOpenSpec,
    manifest_content: bytes,
) -> PinnedInputClosure:
    boundary = project_boundary(root, legacy=root)
    closure = PinnedInputClosure()
    prefix = f"flavor:{definition.coordinate.uri}"
    _pin_project_manifest(closure, root, label_prefix=prefix)
    closure.pin(
        definition_path,
        boundary=boundary,
        label=f"{prefix}:manifest",
        expected_content=manifest_content,
    )
    _pin_loaded_specifications(
        closure,
        root,
        loaded,
        boundary=boundary,
        label_prefix=prefix,
    )
    _pin_definition_references(
        closure,
        root,
        definition,
        boundary=boundary,
        label_prefix=prefix,
    )
    return closure


def _model_mapping(value: object, *, source: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or any(key not in CODING_CLIS for key in value):
        raise CliFailure(
            "generate.invalid_model_selection",
            f"{source} coding models must map supported CLIs to model names",
        )
    try:
        return tuple(
            sorted(
                (
                    key,
                    source_cache_model_selector(
                        model,
                        path=f"{source}.models[{key!r}]",
                    ),
                )
                for key, model in value.items()
            )
        )
    except ContractValidationError as exc:
        raise CliFailure(
            "generate.invalid_model_selection",
            f"{source} coding models must map supported CLIs to model names",
        ) from exc


def _load_toolchain_constraint(
    root: Path,
    reference: ContentReference,
    *,
    boundary: Path,
    source: str,
    contribution: ContributionReference | None = None,
) -> _ResolvedToolchainConstraint:
    if reference.kind != "toolchain-constraint":
        raise CliFailure(
            "generate.invalid_toolchain_constraint",
            f"{source} must use a toolchain-constraint content reference",
        )
    content = _pinned_reference_content(
        root,
        reference,
        boundary=boundary,
        label="toolchain_constraint",
    )
    try:
        constraint = ToolchainConstraint.from_dict(
            json.loads(content.decode("utf-8")), path=f"{source} toolchain constraint"
        )
    except (UnicodeError, json.JSONDecodeError, ContractValidationError) as exc:
        raise CliFailure(
            "generate.invalid_toolchain_constraint",
            f"{source} has an invalid typed toolchain constraint",
        ) from exc
    if contribution is not None and contribution.slot != constraint.toolchain:
        raise CliFailure(
            "generate.invalid_toolchain_constraint",
            f"{source} contribution slot does not match its typed toolchain name",
        )
    return _ResolvedToolchainConstraint(source, reference, constraint, contribution)


def _referenced_toolchain_constraints(
    root: Path,
    authoring_inputs,
    *,
    boundary: Path,
    source: str,
) -> tuple[_ResolvedToolchainConstraint, ...]:
    return tuple(
        _load_toolchain_constraint(
            root,
            reference,
            boundary=boundary,
            source=source,
        )
        for reference in authoring_inputs
        if reference.kind == "toolchain-constraint"
    )


def _load_component_specifications(
    root: Path, definition: ComponentDefinition
) -> LoadedSpecification:
    try:
        return load_specification_provider(
            definition.specification_provider,
            root,
            definition.specification_roots,
            id_prefix=(
                f"{definition.coordinate.namespace}.{definition.coordinate.name}"
            ),
        )
    except LookupError as exc:
        raise CliFailure(
            "generate.unsupported_specification_provider",
            "Component specification_provider is not registered: "
            f"{definition.specification_provider!r}",
        ) from exc


def _loaded_specification_contents(
    loaded: LoadedSpecification,
) -> tuple[tuple[str, str | bytes], ...]:
    context = () if loaded.context_document is None else (loaded.context_document,)
    return (*context, *loaded.contents)


_AUTHORING_PROJECT_SELECTOR_KINDS = frozenset(
    {
        "model-selection",
        "routing-policy",
        "specification-to-source-skill",
        "toolchain-constraint",
        "workflow",
    }
)


def _convergence_content_identity(
    component_root: Path,
    project_root: Path,
    selector: ComponentContentSelector,
) -> str:
    base = (
        project_root
        if selector.kind in _AUTHORING_PROJECT_SELECTOR_KINDS
        else component_root
    )
    configured = base.joinpath(*PurePosixPath(selector.uri).parts)
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise CliFailure(
            "generate.component_authority_input_unavailable",
            f"component.md selector is unavailable: {selector.kind} {selector.uri}",
        ) from exc
    if (
        configured.is_symlink()
        or not resolved.is_relative_to(project_root)
        or not resolved.is_file()
    ):
        raise CliFailure(
            "generate.component_authority_input_unsafe",
            f"component.md selector is unsafe: {selector.kind} {selector.uri}",
        )
    content = _bounded_file_content(
        resolved,
        code="generate.component_authority_input_invalid",
        label=f"component.md {selector.kind} selector",
    )
    identity = "sha256:" + hashlib.sha256(content).hexdigest()
    if selector.pin is not None and selector.pin.uri != identity:
        raise CliFailure(
            "generate.component_authority_pin_mismatch",
            f"component.md selector pin changed: {selector.kind} {selector.uri}",
        )
    return identity


def _legacy_selector_projection(reference: ContentReference) -> dict[str, object]:
    return {"kind": reference.kind, "content_identity": reference.identity.uri}


def _authoring_selector_projection(
    component_root: Path,
    project_root: Path,
    selector: ComponentContentSelector,
) -> dict[str, object]:
    return {
        "kind": selector.kind,
        "content_identity": _convergence_content_identity(
            component_root, project_root, selector
        ),
    }


def _legacy_repository_projection(
    dependency: RepositorySourceDependency,
) -> dict[str, object]:
    return {
        "dependency_id": dependency.dependency_id,
        "repository_url": dependency.repository_url,
        "revision_selector": dependency.revision_selector.to_dict(),
        "dependency_kind": dependency.dependency_kind.value,
        "optional": dependency.optional,
        "integration_contract": (
            None
            if dependency.integration_contract is None
            else _legacy_selector_projection(dependency.integration_contract)
        ),
    }


def _authoring_repository_projection(
    component_root: Path,
    project_root: Path,
    dependency,
) -> dict[str, object]:
    return {
        "dependency_id": dependency.dependency_id,
        "repository_url": dependency.repository_url,
        "revision_selector": dependency.revision_selector.to_dict(),
        "dependency_kind": dependency.dependency_kind.value,
        "optional": dependency.optional,
        "integration_contract": (
            None
            if dependency.integration_contract is None
            else _authoring_selector_projection(
                component_root, project_root, dependency.integration_contract
            )
        ),
    }


def _component_semantic_projections(
    component_root: Path,
    project_root: Path,
    definition: ComponentDefinition,
    authoring: ComponentAuthoring,
) -> tuple[dict[str, object], dict[str, object]]:
    """Project welded and authored values onto one generation-effective shape."""

    legacy_repositories = _component_repository_source_dependencies(
        component_root, definition, boundary=project_root
    )
    specification_identities = [
        {
            "path": relative,
            "content_identity": "sha256:"
            + hashlib.sha256(
                _bounded_file_content(
                    component_root.joinpath(*PurePosixPath(relative).parts),
                    code="generate.component_authority_input_invalid",
                    label="Component specification",
                )
            ).hexdigest(),
        }
        for relative in definition.specification_roots
    ]
    shared_legacy: dict[str, object] = {
        "coordinate": definition.coordinate.uri,
        "version": definition.version,
        "display_name": definition.display_name,
        "profiles": sorted(definition.profiles),
        "sample": definition.sample,
        "requires": [
            item.to_dict()
            for item in sorted(
                definition.requires, key=lambda value: value.requirement_id
            )
        ],
        "specification_provider": definition.specification_provider,
        "specifications": specification_identities,
        "flavor_slots": [
            item.to_dict()
            for item in sorted(definition.flavor_slots, key=lambda value: value.slot_id)
        ],
        "entrypoints": [
            item.to_dict()
            for item in sorted(definition.entrypoints, key=lambda value: value.name)
        ],
    }
    shared_authoring: dict[str, object] = {
        "coordinate": authoring.coordinate.uri,
        "version": authoring.version,
        "display_name": authoring.display_name,
        "profiles": list(authoring.profiles),
        "sample": authoring.sample,
        "requires": [item.to_dict() for item in authoring.requires],
        "specification_provider": authoring.specification_provider,
        "specifications": specification_identities
        if tuple(definition.specification_roots) == authoring.specification_roots
        else [
            {"path": relative, "content_identity": None}
            for relative in authoring.specification_roots
        ],
        "flavor_slots": [item.to_dict() for item in authoring.flavor_slots],
        "entrypoints": [item.to_dict() for item in authoring.entrypoints],
    }
    legacy = {
        **shared_legacy,
        "provides": [
            {
                "name": item.name,
                "version": item.version,
                "interface_identity": (
                    None if item.contract is None else item.contract.uri
                ),
            }
            for item in sorted(definition.provides, key=lambda value: value.name)
        ],
        "authoring_inputs": sorted(
            (_legacy_selector_projection(item) for item in definition.authoring_inputs),
            key=lambda value: (str(value["kind"]), str(value["content_identity"])),
        ),
        "workflow_definition": _legacy_selector_projection(
            definition.workflow_definition
        ),
        "routing_policy": _legacy_selector_projection(definition.routing_policy),
        "acceptance_contracts": sorted(
            (
                _legacy_selector_projection(item)
                for item in definition.acceptance_contracts
            ),
            key=lambda value: (str(value["kind"]), str(value["content_identity"])),
        ),
        "source_dependencies": [
            _legacy_repository_projection(item)
            for item in sorted(
                legacy_repositories, key=lambda value: value.dependency_id
            )
        ],
    }
    authored = {
        **shared_authoring,
        "provides": [
            {
                "name": item.name,
                "version": item.version,
                "interface_identity": (
                    None
                    if item.interface is None
                    else _convergence_content_identity(
                        component_root, project_root, item.interface
                    )
                ),
            }
            for item in authoring.provides
        ],
        "authoring_inputs": sorted(
            (
                _authoring_selector_projection(component_root, project_root, item)
                for item in authoring.authoring_inputs
            ),
            key=lambda value: (str(value["kind"]), str(value["content_identity"])),
        ),
        "workflow_definition": _authoring_selector_projection(
            component_root, project_root, authoring.workflow_definition
        ),
        "routing_policy": _authoring_selector_projection(
            component_root, project_root, authoring.routing_policy
        ),
        "acceptance_contracts": sorted(
            (
                _authoring_selector_projection(component_root, project_root, item)
                for item in authoring.acceptance_contracts
            ),
            key=lambda value: (str(value["kind"]), str(value["content_identity"])),
        ),
        "source_dependencies": [
            _authoring_repository_projection(component_root, project_root, item)
            for item in authoring.source_dependencies
        ],
    }
    return legacy, authored


def load_component_authority_convergence(
    component_root: Path,
    *,
    project_root: Path | None = None,
) -> ComponentAuthorityConvergence:
    """Load legacy authority and fail if colocated readable intent changes behavior."""

    root = Path(component_root).resolve(strict=True)
    boundary = (
        project_boundary(root, legacy=root.parent)
        if project_root is None
        else Path(project_root).resolve(strict=True)
    )
    legacy_path = root / "component.json"
    if legacy_path.is_symlink() or not legacy_path.is_file():
        raise CliFailure(
            "generate.component_required",
            "a specification directory must contain component.json until lock cutover",
        )
    try:
        legacy_content = _bounded_file_content(
            legacy_path,
            code="generate.invalid_component",
            label="Component definition",
        )
        definition = ComponentDefinition.from_dict(
            json.loads(legacy_content.decode("utf-8"))
        )
    except (UnicodeError, TypeError, ValueError) as exc:
        raise CliFailure(
            "generate.invalid_component", "Component definition is invalid"
        ) from exc
    authoring_path = root / "component.md"
    if not authoring_path.exists() and not authoring_path.is_symlink():
        return ComponentAuthorityConvergence(
            definition, legacy_content, None, None, None
        )
    if authoring_path.is_symlink() or not authoring_path.is_file():
        raise CliFailure(
            "generate.component_authority_invalid",
            "component.md must be a regular file",
        )
    try:
        authoring_content = _bounded_file_content(
            authoring_path,
            code="generate.component_authority_invalid",
            label="Component authoring",
        )
        authoring = parse_component_markdown(
            authoring_path,
            authoring_content.decode("utf-8"),
            project_root=boundary,
        )
    except (ComponentMarkdownError, UnicodeError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "generate.component_authority_invalid")
        raise CliFailure(str(code), "Component authoring is invalid") from exc
    legacy, authored = _component_semantic_projections(
        root, boundary, definition, authoring
    )
    mismatches = tuple(
        key
        for key in sorted(set(legacy) | set(authored))
        if legacy.get(key) != authored.get(key)
    )
    if mismatches:
        raise CliFailure(
            "generate.component_authority_mismatch",
            "component.md and component.json differ in generation semantics: "
            + ", ".join(mismatches),
        )
    semantic_identity = canonical_identity(
        {
            "schema": "literate-ai/component-authority-convergence@1",
            "projection": authored,
        }
    )
    return ComponentAuthorityConvergence(
        definition,
        legacy_content,
        authoring,
        authoring_content,
        semantic_identity,
    )


def _component_documents(
    specification: Path,
) -> tuple[
    Path,
    ComponentDefinition,
    LoadedOpenSpec,
    tuple[RecipeDocument, ...],
    tuple[tuple[str, str], ...],
    tuple[RecipeSkill, ...],
    tuple[_ResolvedToolchainConstraint, ...],
    PinnedInputClosure,
]:
    resolved = specification.resolve(strict=True)
    if resolved.is_file():
        raise CliFailure(
            "generate.component_required",
            "source generation requires a Component directory so skill, workflow, "
            "routing, and specification inputs are content-pinned",
        )
    convergence = load_component_authority_convergence(resolved)
    manifest_content = convergence.legacy_content
    definition = convergence.definition
    boundary = project_boundary(resolved, legacy=resolved.parent)
    _component_repository_source_dependencies(resolved, definition, boundary=boundary)
    for reference in definition.authoring_inputs:
        if reference.kind in {
            "model-selection",
            "specification-to-source-skill",
            "toolchain-constraint",
        }:
            continue
        _pinned_reference_content(
            resolved,
            reference,
            boundary=boundary,
            label=reference.kind,
        )
    try:
        loaded = _load_component_specifications(resolved, definition)
        loaded.require_unchanged(resolved)
    except SPECIFICATION_PROVIDER_ERRORS as exc:
        raise CliFailure(
            "generate.specification_invalid",
            "Component specification provider rejected an authored root",
        ) from exc
    source_documents = tuple(
        RecipeDocument.create(path, _text(content))
        for path, content in _loaded_specification_contents(loaded)
    )
    specification_documents = source_documents
    acceptance_documents: list[RecipeDocument] = []
    for index, reference in enumerate(definition.acceptance_contracts):
        content = _pinned_reference_content(
            resolved,
            reference,
            boundary=boundary,
            label="acceptance_contract",
        )
        configured = Path(reference.uri)
        path = (
            configured.as_posix()
            if not configured.is_absolute()
            and ".." not in configured.parts
            and "." not in configured.parts
            else f"acceptance/{index}-{configured.name}"
        )
        acceptance_documents.append(RecipeDocument.create(path, _text(content)))
    documents = (*specification_documents, *acceptance_documents)
    application: object = next(
        (
            json.loads(document.content)
            for document in documents
            if document.path == "openspec/app.json"
        ),
        {},
    )
    if not isinstance(application, dict):
        raise CliFailure(
            "generate.invalid_specification",
            "openspec/app.json must contain a JSON object",
        )
    if application.get("schema") == PORTABLE_APPLICATION_SCHEMA:
        application = validate_portable_application(application)
    application_models = _model_mapping(application.get("models"), source="application")
    referenced_models = _referenced_models(
        resolved,
        definition.authoring_inputs,
        source="Component",
    )
    if application_models and referenced_models:
        raise CliFailure(
            "generate.invalid_model_selection",
            "Component has multiple coding-model specifications",
        )
    models = referenced_models or application_models
    skills = _referenced_skills(
        resolved,
        definition.authoring_inputs,
        source=f"Component {definition.coordinate.uri}",
        boundary=boundary,
    )
    if not skills:
        raise CliFailure(
            "generate.skill_required",
            "Component source generation requires a pinned "
            "specification-to-source skill",
        )
    toolchain_constraints = _referenced_toolchain_constraints(
        resolved,
        definition.authoring_inputs,
        boundary=boundary,
        source=f"Component {definition.coordinate.uri}",
    )
    input_closure = _component_authority_closure(
        resolved, definition, loaded, manifest_content
    )
    if convergence.authoring_content is not None:
        input_closure.pin(
            resolved / "component.md",
            boundary=boundary,
            label=f"component:{definition.coordinate.uri}:authoring",
            expected_content=convergence.authoring_content,
        )
    return (
        resolved,
        definition,
        loaded,
        documents,
        models,
        skills,
        toolchain_constraints,
        input_closure,
    )


def _referenced_models(
    root: Path,
    authoring_inputs,
    *,
    source: str,
) -> tuple[tuple[str, str], ...]:
    references = tuple(
        item for item in authoring_inputs if item.kind == "model-selection"
    )
    if len(references) > 1:
        raise CliFailure(
            "generate.invalid_model_selection",
            f"a {source} may provide at most one coding-model specification",
        )
    if not references:
        return ()
    reference = references[0]
    configured_path = root / reference.uri
    path = configured_path.resolve()
    if (
        not path.is_relative_to(root.resolve())
        or configured_path.is_symlink()
        or not path.is_file()
    ):
        raise CliFailure(
            "generate.invalid_model_selection",
            f"{source} coding-model specification is unavailable",
        )
    content = _bounded_file_content(
        path,
        code="generate.invalid_model_selection",
        label=f"{source} coding-model specification",
    )
    if hashlib.sha256(content).hexdigest() != reference.identity.digest:
        raise CliFailure(
            "generate.invalid_model_selection",
            f"{source} coding-model specification identity changed",
        )
    value = json.loads(content)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "models"}
        or value.get("schema") != MODEL_SELECTION_SCHEMA
    ):
        raise CliFailure(
            "generate.invalid_model_selection",
            f"{source} coding-model specification schema is unsupported",
        )
    return _model_mapping(value.get("models"), source=source)


def _referenced_skills(
    root: Path,
    authoring_inputs,
    *,
    source: str,
    boundary: Path,
) -> tuple[RecipeSkill, ...]:
    references = tuple(
        item
        for item in authoring_inputs
        if item.kind == "specification-to-source-skill"
    )
    skills: list[RecipeSkill] = []
    resolved_boundary = boundary.resolve(strict=True)
    project = discover_project(root)
    catalog_paths = (
        frozenset(specification_to_source_skill_paths(project))
        if project is not None
        else None
    )
    for reference in references:
        configured_path = root / reference.uri
        path = configured_path.resolve()
        if (
            not path.is_relative_to(resolved_boundary)
            or configured_path.is_symlink()
            or not path.is_file()
        ):
            raise CliFailure(
                "generate.invalid_skill",
                f"{source} specification-to-source skill is unavailable",
            )
        if catalog_paths is not None and path not in catalog_paths:
            raise CliFailure(
                "generate.skill_not_cataloged",
                f"{source} specification-to-source skill is outside the declared "
                "project skill catalog",
            )
        try:
            skills.append(
                RecipeSkill.from_reference(
                    reference,
                    _bounded_file_content(
                        path,
                        code="generate.invalid_skill",
                        label=f"{source} specification-to-source skill",
                    ),
                    source=source,
                )
            )
        except (OSError, CodingCliError, ContractValidationError) as exc:
            raise CliFailure(
                "generate.invalid_skill",
                f"{source} specification-to-source skill is invalid",
            ) from exc
    return tuple(skills)


def _flavor_models(
    root: Path, definition: FlavorDefinition
) -> tuple[tuple[str, str], ...]:
    return _referenced_models(
        root,
        definition.authoring_inputs,
        source="Flavor",
    )


def _flavor_skills(root: Path, definition: FlavorDefinition) -> tuple[RecipeSkill, ...]:
    return _referenced_skills(
        root,
        definition.authoring_inputs,
        source=f"Flavor {definition.coordinate.uri}",
        boundary=project_boundary(root, legacy=root),
    )


def _reject_duplicate_canonical_flavors(
    entries: Sequence[tuple[Path, str, str]],
) -> None:
    """Fail closed when two Flavor directories share one canonical coordinate."""

    from literate_ai.adapters.project_initialization import (
        canonical_flavor_catalog_collisions,
    )

    collisions = canonical_flavor_catalog_collisions(
        tuple(
            (definition_path.parent.as_posix(), flavor_id, coordinate_uri)
            for definition_path, flavor_id, coordinate_uri in entries
        )
    )
    if not collisions:
        return
    identity, locations = collisions[0]
    listed = " and ".join(locations)
    raise CliFailure(
        "generate.duplicate_flavor_alias",
        f"Flavor catalog has duplicate authority for {identity}: {listed}",
    )


def load_legacy_flavor_catalog_entries(
    roots: Sequence[Path],
) -> tuple[_LoadedFlavor, ...]:
    definition_paths: set[Path] = set()
    for configured in roots:
        if configured.is_symlink():
            raise CliFailure(
                "generate.invalid_flavor", "Flavor root cannot be a symlink"
            )
        root = configured.resolve(strict=True)
        direct = tuple(
            candidate
            for candidate in (root / "flavor.md", root / "flavor.json")
            if candidate.is_file()
        )
        if len(direct) > 1:
            raise CliFailure(
                "generate.ambiguous_flavor",
                "Flavor entry cannot contain both flavor.md and flavor.json",
            )
        if direct:
            definition_paths.add(direct[0])
        else:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                candidates = tuple(
                    candidate
                    for candidate in (child / "flavor.md", child / "flavor.json")
                    if candidate.is_file()
                )
                if len(candidates) > 1:
                    raise CliFailure(
                        "generate.ambiguous_flavor",
                        "Flavor entry cannot contain both flavor.md and flavor.json",
                    )
                for candidate in candidates:
                    if candidate.parent.is_symlink():
                        raise CliFailure(
                            "generate.invalid_flavor",
                            "Flavor catalog entries cannot be symlinks",
                        )
                    definition_paths.add(candidate)
    flavors: list[_LoadedFlavor] = []
    loaded_paths: list[Path] = []
    for definition_path in sorted(definition_paths):
        if definition_path.is_symlink() or not definition_path.is_file():
            raise CliFailure(
                "generate.invalid_flavor", "Flavor definition must be a regular file"
            )
        root = definition_path.parent
        manifest_content = _bounded_file_content(
            definition_path,
            code="generate.invalid_flavor",
            label="Flavor definition",
        )
        boundary = project_boundary(root, legacy=root)
        try:
            if definition_path.name == "flavor.md":
                authoring = parse_flavor_markdown(
                    manifest_content, source=definition_path.as_posix()
                )
                definition = authoring.resolve(
                    lambda uri, flavor_root=root, project_root=boundary: (
                        _authored_flavor_reference_content(
                            flavor_root, uri, boundary=project_root
                        )
                    )
                )
            else:
                definition = FlavorDefinition.from_dict(
                    json.loads(manifest_content.decode("utf-8"))
                )
        except (
            FlavorMarkdownError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise CliFailure(
                "generate.invalid_flavor", "Flavor definition is invalid"
            ) from exc
        if len(definition.supported_targets) != 1:
            raise CliFailure(
                "generate.invalid_flavor", "generation Flavors must select one value"
            )
        loaded = OpenSpecProvider().load(
            root, tuple(item.uri for item in definition.specification_fragments)
        )
        expected = tuple(item.identity for item in definition.specification_fragments)
        actual = tuple(item.identity for item in loaded.specification_set.artifacts)
        if actual != expected:
            raise CliFailure(
                "generate.flavor_specification_drift",
                f"Flavor specification identity changed: {definition.coordinate.name}",
            )
        loaded.require_unchanged(root)
        for reference in definition.authoring_inputs:
            if reference.kind in {
                "model-selection",
                "specification-to-source-skill",
            }:
                continue
            _pinned_reference_content(
                root,
                reference,
                boundary=boundary,
                label=reference.kind,
            )
        try:
            toolchain_constraints = load_flavor_contributions(
                root, definition, boundary=boundary
            )
        except GenerationPreparationError as exc:
            raise CliFailure(exc.code, str(exc)) from exc
        prefix = f"flavors/{definition.coordinate.name}"
        revision = FlavorRevision(
            definition,
            ContentIdentity(
                HashAlgorithm.SHA256, hashlib.sha256(manifest_content).hexdigest()
            )
            if definition_path.name == "flavor.md"
            else None,
            (),
        )
        descriptor = FlavorDescriptor(
            revision.identity,
            definition,
            definition.supported_targets[0],
        )
        recipe = RecipeFlavor(
            definition.coordinate.name,
            definition.primary_axis.value,
            definition.supported_targets[0],
            tuple(
                RecipeDocument.create(f"{prefix}/{path}", _text(content))
                for path, content in loaded.contents
            ),
            _flavor_models(root, definition),
            definition.conflicts,
            definition.coordinate.uri,
            _flavor_skills(root, definition),
            revision_identity=revision.identity.uri,
            specification_set_identity=loaded.specification_set.identity.uri,
        )
        flavors.append(
            _LoadedFlavor(
                recipe,
                definition,
                loaded.specification_set,
                revision,
                descriptor,
                _flavor_authority_closure(
                    root, definition_path, definition, loaded, manifest_content
                ),
                toolchain_constraints,
            )
        )
        loaded_paths.append(definition_path)
    _reject_duplicate_canonical_flavors(
        tuple(
            (path, loaded.recipe.flavor_id, loaded.recipe.coordinate_uri)
            for path, loaded in zip(loaded_paths, flavors, strict=True)
        )
    )
    identities = tuple(item.recipe.flavor_id for item in flavors)
    coordinates = tuple(item.recipe.coordinate_uri for item in flavors)
    if len(set(identities)) != len(identities) or len(set(coordinates)) != len(
        coordinates
    ):
        raise CliFailure(
            "generate.invalid_flavor",
            "Flavor catalog IDs and coordinates must be unique",
        )
    return tuple(flavors)


def load_flavor_catalog(roots: Sequence[Path]) -> tuple[RecipeFlavor, ...]:
    """Load the public prompt-facing view of a verified Flavor catalog."""

    return tuple(item.recipe for item in load_legacy_flavor_catalog_entries(roots))


# Public result and service names; callers never depend on adapter-private CLI helpers.
PreparedGeneration = _PreparedGeneration
LoadedFlavor = _LoadedFlavor
ResolvedToolchainConstraint = _ResolvedToolchainConstraint
EffectiveToolchainConstraint = _EffectiveToolchainConstraint
load_component_generation_documents = _component_documents
load_component_repository_source_dependencies = (
    _component_repository_source_dependencies
)
bounded_file_content = _bounded_file_content
model_mapping = _model_mapping
