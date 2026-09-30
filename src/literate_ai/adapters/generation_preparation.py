"""Filesystem/model adapter implementing locked generation preparation."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote

from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.adapters.models import (
    CODING_CLIS,
    INHERITED_SESSION_PROVIDER,
    PORTABLE_APPLICATION_SCHEMA,
    CodingCliError,
    CodingCliSelection,
    GenerationRecipe,
    PortableApplicationError,
    RecipeCppLibraryBuild,
    RecipeDeploymentUnit,
    RecipeDocument,
    RecipeFlavor,
    RecipeLibraryDependency,
    RecipeSkill,
    portable_source_entrypoint,
    validate_portable_application,
)
from literate_ai.application.component_generation_context import PromptSegmentInput
from literate_ai.application.component_generation_preparation import (
    ComponentGenerationWorkspaceDescriptor,
    LockedNodeGenerationProjection,
    PreparedComponentGenerationNode,
)
from literate_ai.application.generation_preparation import (
    GenerationPreparationError,
    GenerationPreparationRequest,
    GenerationPreparationService,
)
from literate_ai.application.library_artifacts import (
    project_cpp_library_layout,
    project_library_import_surface,
)
from literate_ai.application.models import GenerationExecutionPlan
from literate_ai.application.planning import compile_generation_execution_plan
from literate_ai.application.skill_closure import (
    SkillClosureError,
    load_admitted_skill_catalog,
)
from literate_ai.contracts import (
    REPOSITORY_SOURCE_DEPENDENCY_CONTENT_KIND,
    STANDARD_COMMAND_PROFILE_CONTENT_KIND,
    AuthoredBinaryAsset,
    Capability,
    ComponentAuthoring,
    ComponentDefinition,
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    ContributionKind,
    ContributionReference,
    CycloneDxManagedGraph,
    DependencyKind,
    Entrypoint,
    FlavorAxis,
    FlavorCardinality,
    LockedComponentRevision,
    MergeOperator,
    ModelScopeBinding,
    ModelScopeKind,
    RepositorySourceDependency,
    StandardBuildSystemCommandProfile,
    StandardLanguageCommandProfile,
    StandardPlatformCommandProfile,
    ToolchainConstraint,
    merge_toolchain_constraints,
    parse_standard_command_profile,
    project_component_lock_managed_graph,
    resolve_model_scope,
    source_cache_model_selector,
    standard_entrypoint_source_path,
)
from literate_ai.contracts.component_locking import ordered_specification_set_identity
from literate_ai.contracts.executable_components import (
    ComponentGenerationPlan,
    ContextAuthorityKind,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.native_sdks import (
    NATIVE_SDK_BUILD_RECIPE_CONTENT_KIND,
    NativeSdkBuildRecipe,
)
from literate_ai.models import Locality, ModelEndpoint
from literate_ai.projects import PinnedInputClosure, project_boundary
from literate_ai.spec_map import (
    with_debug_skill_identities,
)

MODEL_SELECTION_SCHEMA = "literate-ai/coding-model-selection@1"
MAXIMUM_PINNED_INPUT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResolvedToolchainConstraint:
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
class EffectiveToolchainConstraint:
    constraint: ToolchainConstraint
    sources: tuple[ResolvedToolchainConstraint, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "constraint": self.constraint.to_dict(),
            "sources": [item.to_dict() for item in self.sources],
        }


@dataclass(frozen=True, slots=True)
class PreparedLockedGeneration:
    component_root: Path
    boundary: Path
    definition: ComponentDefinition
    recipe: GenerationRecipe
    selected_flavors: tuple[RecipeFlavor, ...]
    component_revision: LockedComponentRevision
    toolchain_constraints: tuple[EffectiveToolchainConstraint, ...]
    input_closure: PinnedInputClosure
    flavor_catalog_audit_identity: ContentIdentity
    locked_authority_snapshot: LockedGenerationAuthoritySnapshot
    native_sdk_inputs: object | None = None


def _error(code: str, message: str) -> None:
    raise GenerationPreparationError(code, message)


def _read_reference(
    root: Path,
    reference: ContentReference,
    *,
    boundary: Path,
    label: str,
) -> bytes:
    configured = root / reference.uri
    try:
        path = configured.resolve(strict=True)
    except OSError as exc:
        raise GenerationPreparationError(
            f"plan.{label}_unavailable", f"pinned {label} is unavailable"
        ) from exc
    if (
        configured.is_symlink()
        or not path.is_file()
        or not path.is_relative_to(boundary.resolve(strict=True))
    ):
        _error(
            f"plan.{label}_unavailable",
            f"pinned {label} must be a regular file inside the project boundary",
        )
    try:
        with path.open("rb") as stream:
            content = stream.read(MAXIMUM_PINNED_INPUT_BYTES + 1)
    except OSError as exc:
        raise GenerationPreparationError(
            f"plan.{label}_unavailable", f"pinned {label} is unavailable"
        ) from exc
    if len(content) > MAXIMUM_PINNED_INPUT_BYTES:
        _error(
            f"plan.{label}_unavailable",
            f"pinned {label} exceeds the pinned-input size limit",
        )
    if hashlib.sha256(content).hexdigest() != reference.identity.digest:
        _error(f"plan.{label}_drift", f"pinned {label} identity changed")
    return content


def _text(content: bytes, *, source: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeError:
        _error("generate.locked_input_non_utf8", f"{source} must contain UTF-8 text")


def _model_mapping(value: object, *, source: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or any(key not in CODING_CLIS for key in value):
        _error(
            "generate.invalid_model_selection",
            f"{source} coding models must map supported CLIs to model names",
        )
    try:
        return tuple(
            sorted(
                (
                    key,
                    source_cache_model_selector(
                        model, path=f"{source}.models[{key!r}]"
                    ),
                )
                for key, model in value.items()
            )
        )
    except ContractValidationError:
        _error(
            "generate.invalid_model_selection",
            f"{source} coding models must map supported CLIs to model names",
        )


def _models(
    references: tuple[ContentReference, ...],
    *,
    read: Callable[[ContentReference], bytes],
    source: str,
) -> tuple[tuple[str, str], ...]:
    selected = tuple(item for item in references if item.kind == "model-selection")
    if len(selected) > 1:
        _error(
            "generate.invalid_model_selection",
            f"a {source} may provide at most one coding-model specification",
        )
    if not selected:
        return ()
    try:
        value = json.loads(read(selected[0]).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _error(
            "generate.invalid_model_selection",
            f"{source} coding-model specification is invalid",
        )
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "models"}
        or value.get("schema") != MODEL_SELECTION_SCHEMA
    ):
        _error(
            "generate.invalid_model_selection",
            f"{source} coding-model specification schema is unsupported",
        )
    return _model_mapping(value.get("models"), source=source)


def _skills(
    references: tuple[ContentReference, ...],
    *,
    read: Callable[[ContentReference], bytes],
    source: str,
) -> tuple[RecipeSkill, ...]:
    skills: list[RecipeSkill] = []
    for reference in references:
        if reference.kind != "specification-to-source-skill":
            continue
        try:
            skills.append(
                RecipeSkill.from_reference(reference, read(reference), source=source)
            )
        except (CodingCliError, ContractValidationError):
            _error(
                "generate.invalid_skill",
                f"{source} specification-to-source skill is invalid",
            )
    if len({skill.skill_id for skill in skills}) != len(skills):
        return tuple(skills)
    pending = {skill.skill_id: skill for skill in skills}
    ordered: list[RecipeSkill] = []
    while pending:
        ready = [
            skill
            for skill in pending.values()
            if all(
                dependency.skill_id not in pending for dependency in skill.dependencies
            )
        ]
        if not ready:
            return tuple(skills)
        for skill in sorted(ready, key=lambda item: item.skill_id):
            ordered.append(skill)
            del pending[skill.skill_id]
    return tuple(ordered)


def _admitted_skill_catalog(
    snapshot: LockedGenerationAuthoritySnapshot,
) -> tuple[RecipeSkill, ...]:
    catalog = getattr(snapshot, "_catalog", None)
    project = getattr(catalog, "project", None)
    if project is None:
        return ()
    try:
        return load_admitted_skill_catalog(project)
    except SkillClosureError as exc:
        _error(exc.code.replace("skill_closure.", "generate."), exc.message)


def _toolchain_constraint(
    reference: ContentReference,
    *,
    content: bytes,
    source: str,
    contribution: ContributionReference | None = None,
) -> ResolvedToolchainConstraint:
    if reference.kind != "toolchain-constraint":
        _error(
            "generate.invalid_toolchain_constraint",
            f"{source} must use a toolchain-constraint content reference",
        )
    try:
        constraint = ToolchainConstraint.from_dict(
            json.loads(content.decode("utf-8")),
            path=f"{source} toolchain constraint",
        )
    except (UnicodeError, json.JSONDecodeError, ContractValidationError):
        _error(
            "generate.invalid_toolchain_constraint",
            f"{source} has an invalid typed toolchain constraint",
        )
    if contribution is not None and contribution.slot != constraint.toolchain:
        _error(
            "generate.invalid_toolchain_constraint",
            f"{source} contribution slot does not match its typed toolchain name",
        )
    return ResolvedToolchainConstraint(source, reference, constraint, contribution)


def load_flavor_contributions(
    root: Path,
    definition,
    *,
    boundary: Path,
) -> tuple[ResolvedToolchainConstraint, ...]:
    """Verify one authored Flavor's typed, content-pinned contributions.

    This is the public filesystem adapter surface used by legacy catalog validation;
    contribution semantics do not belong to a command-line module.
    """

    resolved: list[ResolvedToolchainConstraint] = []
    for contribution in definition.contributions:
        source = (
            f"Flavor {definition.coordinate.uri} contribution "
            f"{contribution.contribution_id}"
        )
        if contribution.content.kind == NATIVE_SDK_BUILD_RECIPE_CONTENT_KIND:
            try:
                recipe = NativeSdkBuildRecipe.from_dict(
                    json.loads(
                        _read_reference(
                            root,
                            contribution.content,
                            boundary=boundary,
                            label="native_sdk_build_recipe",
                        )
                    )
                )
                if (
                    contribution.kind is not ContributionKind.BUILDER
                    or contribution.merge_operator is not MergeOperator.EXACT_SINGLETON
                    or contribution.slot != f"native-sdk:{recipe.dependency_id}"
                ):
                    raise ValueError(
                        "SDK recipe requires its exact dependency builder slot"
                    )
            except (TypeError, ValueError, UnicodeError) as exc:
                raise GenerationPreparationError(
                    "generate.invalid_native_sdk_build_recipe",
                    f"{source} has an invalid native SDK build recipe",
                ) from exc
            continue
        if contribution.kind is ContributionKind.SOURCE_DEPENDENCY:
            if contribution.content.kind != REPOSITORY_SOURCE_DEPENDENCY_CONTENT_KIND:
                _error(
                    "generate.invalid_repository_source_dependency",
                    f"{source} must use a repository-source-dependency "
                    "content reference",
                )
            content = _read_reference(
                root,
                contribution.content,
                boundary=boundary,
                label="repository_source_dependency",
            )
            try:
                dependency = RepositorySourceDependency.from_dict(
                    json.loads(content.decode("utf-8")), path=source
                )
            except (UnicodeError, json.JSONDecodeError, ContractValidationError) as exc:
                raise GenerationPreparationError(
                    "generate.invalid_repository_source_dependency",
                    f"{source} has an invalid repository source dependency",
                ) from exc
            if dependency.dependency_id != contribution.contribution_id:
                _error(
                    "generate.repository_source_dependency_key_mismatch",
                    "repository source contribution ID must equal its declared "
                    "dependency_id so keyed-union composition has one stable key",
                )
            if dependency.integration_contract is not None:
                _read_reference(
                    root,
                    dependency.integration_contract,
                    boundary=boundary,
                    label="repository_source_integration_contract",
                )
            continue
        if contribution.kind is not ContributionKind.TOOLCHAIN:
            _read_reference(
                root,
                contribution.content,
                boundary=boundary,
                label=f"flavor_contribution_{contribution.contribution_id}",
            )
            continue
        resolved.append(
            _toolchain_constraint(
                contribution.content,
                content=_read_reference(
                    root,
                    contribution.content,
                    boundary=boundary,
                    label="toolchain_constraint",
                ),
                source=source,
                contribution=contribution,
            )
        )
    return tuple(resolved)


def _merge_toolchains(
    sources: Sequence[ResolvedToolchainConstraint],
) -> tuple[EffectiveToolchainConstraint, ...]:
    by_toolchain: dict[str, list[ResolvedToolchainConstraint]] = {}
    for item in sources:
        by_toolchain.setdefault(item.constraint.toolchain, []).append(item)
    effective: list[EffectiveToolchainConstraint] = []
    for toolchain in sorted(by_toolchain):
        selected_sources = tuple(by_toolchain[toolchain])
        try:
            constraint = merge_toolchain_constraints(
                tuple(item.constraint for item in selected_sources),
                path=f"effective toolchain {toolchain}",
            )
        except ContractValidationError:
            _error(
                "generate.toolchain_constraint_conflict",
                f"Component and selected Flavors have incompatible {toolchain!r} "
                "toolchain constraints",
            )
        effective.append(EffectiveToolchainConstraint(constraint, selected_sources))
    return tuple(effective)


def _component_definition(
    authoring: ComponentAuthoring,
    revision: LockedComponentRevision,
    *,
    native_sdk_input_identities: tuple[ContentIdentity, ...] = (),
) -> ComponentDefinition:
    if len(revision.repository_sources) != len(native_sdk_input_identities):
        raise GenerationPreparationError(
            "generate.repository_source_admission_required",
            "repository source locks require SDK build admission before generation",
        )
    interfaces = {(item.kind, item.uri): item for item in revision.public_interfaces}
    provides = tuple(
        Capability(
            item.name,
            item.version,
            (
                None
                if item.interface is None
                else interfaces[(item.interface.kind, item.interface.uri)].identity
            ),
        )
        for item in authoring.provides
    )
    return ComponentDefinition(
        coordinate=authoring.coordinate,
        version=authoring.version,
        display_name=authoring.display_name,
        description=authoring.description,
        profiles=authoring.profiles,
        sample=authoring.sample,
        provides=provides,
        requires=authoring.requires,
        specification_provider=authoring.specification_provider,
        specification_roots=authoring.specification_roots,
        authoring_inputs=revision.authoring_inputs,
        workflow_definition=revision.workflow_definition,
        routing_policy=revision.routing_policy,
        flavor_slots=authoring.flavor_slots,
        entrypoints=authoring.entrypoints,
        acceptance_contracts=revision.acceptance_contracts,
        source_dependencies=(),
    )


def _documents_for_node(
    snapshot: LockedGenerationAuthoritySnapshot,
    component_revision: ContentIdentity,
) -> tuple[RecipeDocument, ...]:
    authority = snapshot.authority
    lock = authority.lock
    nodes = {item.revision.identity.uri: item for item in lock.nodes}
    selected_node = nodes[component_revision.uri]
    raw: list[tuple[str, bytes]] = [
        (
            reference.uri,
            snapshot.component_content(
                selected_node.revision.authoring_identity, reference
            ),
        )
        for reference in selected_node.revision.specifications
    ]
    raw.extend(
        (
            reference.uri,
            snapshot.component_content(
                selected_node.revision.authoring_identity, reference
            ),
        )
        for reference in selected_node.revision.acceptance_contracts
    )
    for edge in lock.edges:
        if (
            edge.consumer_revision != component_revision
            or edge.kind is not DependencyKind.GENERATION
        ):
            continue
        provider = nodes[edge.provider_revision.uri]
        matches = tuple(
            reference
            for reference in provider.revision.public_interfaces
            if reference.identity == edge.public_interface_identity
        )
        if len(matches) != 1:
            _error(
                "generate.dependency_interface_unavailable",
                "a direct generation dependency lacks its exact locked public "
                "interface contract",
            )
        reference = matches[0]
        raw.append(
            (
                f"dependency-interfaces/{provider.revision.identity.digest}/{reference.uri}",
                snapshot.component_content(
                    provider.revision.authoring_identity, reference
                ),
            )
        )
    unique: dict[str, RecipeDocument] = {}
    for path, content in raw:
        if path in unique:
            continue
        unique[path] = RecipeDocument.create(
            path, _text(content, source=f"locked generation input {path}")
        )
    return tuple(unique.values())


def _documents(
    snapshot: LockedGenerationAuthoritySnapshot,
) -> tuple[RecipeDocument, ...]:
    return _documents_for_node(snapshot, snapshot.authority.lock.root_revision)


def _flavors_for_node(
    snapshot: LockedGenerationAuthoritySnapshot,
    component_revision: ContentIdentity,
) -> tuple[RecipeFlavor, ...]:
    authority = snapshot.authority
    root_node = next(
        node
        for node in authority.lock.nodes
        if node.revision.identity == component_revision
    )
    selected_root = {item.uri for item in root_node.revision.selected_flavor_revisions}
    slots: dict[str, set[str]] = {}
    for resolution in root_node.target_flavor_selection.slots:
        for selected in resolution.selected:
            slots.setdefault(selected.flavor_revision.uri, set()).add(
                resolution.slot.slot_id
            )
    result: list[RecipeFlavor] = []
    for revision in authority.selected_flavors:
        if revision.identity.uri not in selected_root:
            continue
        definition = revision.definition

        def read(
            reference: ContentReference,
            flavor_identity: ContentIdentity = revision.identity,
        ) -> bytes:
            return snapshot.flavor_content(flavor_identity, reference)

        result.append(
            RecipeFlavor(
                flavor_id=definition.coordinate.name,
                axis=definition.primary_axis.value,
                value=definition.supported_targets[0],
                documents=tuple(
                    RecipeDocument.create(
                        f"flavors/{definition.coordinate.name}/{reference.uri}",
                        _text(
                            read(reference),
                            source=(
                                f"Flavor {definition.coordinate.uri} input "
                                f"{reference.uri}"
                            ),
                        ),
                    )
                    for reference in definition.specification_fragments
                ),
                models=_models(
                    definition.authoring_inputs,
                    read=read,
                    source=f"Flavor {definition.coordinate.uri}",
                ),
                conflicts=definition.conflicts,
                coordinate_uri=definition.coordinate.uri,
                skills=_skills(
                    definition.authoring_inputs,
                    read=read,
                    source=f"Flavor {definition.coordinate.uri}",
                ),
                revision_identity=revision.identity.uri,
                specification_set_identity=ordered_specification_set_identity(
                    "openspec", definition.specification_fragments
                ).uri,
                slot_ids=tuple(sorted(slots.get(revision.identity.uri, ()))),
            )
        )
    return tuple(result)


def _flavors(snapshot: LockedGenerationAuthoritySnapshot) -> tuple[RecipeFlavor, ...]:
    return _flavors_for_node(snapshot, snapshot.authority.lock.root_revision)


def _component_models_for_node(
    snapshot: LockedGenerationAuthoritySnapshot,
    revision: LockedComponentRevision,
    documents: tuple[RecipeDocument, ...],
) -> tuple[tuple[str, str], ...]:
    application: object = next(
        (
            json.loads(document.content)
            for document in documents
            if document.path == "openspec/app.json"
        ),
        {},
    )
    if not isinstance(application, dict):
        _error(
            "generate.invalid_specification",
            "openspec/app.json must contain a JSON object",
        )
    if application.get("schema") == PORTABLE_APPLICATION_SCHEMA:
        application = validate_portable_application(application)
    application_models = _model_mapping(application.get("models"), source="application")
    referenced_models = _models(
        revision.authoring_inputs,
        read=lambda reference: snapshot.component_content(
            revision.authoring_identity, reference
        ),
        source=f"Component {revision.coordinate.uri}",
    )
    if application_models and referenced_models:
        _error(
            "generate.invalid_model_selection",
            "Component has multiple coding-model specifications",
        )
    return referenced_models or application_models


class LockedComponentModelSelectionAdapter:
    """Resolve each locked node's effective provider-visible model binding."""

    def __init__(self, *, pipeline_model: str | None = None) -> None:
        self.pipeline_model = pipeline_model

    def bindings(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        *,
        coding_cli: str,
    ) -> dict[str, ModelScopeBinding]:
        if coding_cli not in (*CODING_CLIS, INHERITED_SESSION_PROVIDER):
            raise ValueError("coding_cli must identify a supported generation provider")
        snapshot.require_unchanged()
        pipeline_owner = canonical_identity(
            {
                "schema": "literate-ai/pipeline-model-scope-owner@1",
                "provider_id": coding_cli,
                "configured_selector": self.pipeline_model,
            }
        )
        pipeline = resolve_model_scope(
            scope_kind=ModelScopeKind.PIPELINE,
            owner_identity=pipeline_owner,
            provider_id=coding_cli,
            candidates=(
                ()
                if self.pipeline_model is None
                else ((pipeline_owner, self.pipeline_model),)
            ),
        )
        result: dict[str, ModelScopeBinding] = {}
        for node in sorted(
            snapshot.authority.lock.nodes,
            key=lambda item: item.revision.identity.uri,
        ):
            revision = node.revision
            documents = _documents_for_node(snapshot, revision.identity)
            component_models = _component_models_for_node(snapshot, revision, documents)
            component_model = dict(component_models).get(coding_cli)
            component = resolve_model_scope(
                scope_kind=ModelScopeKind.COMPONENT,
                owner_identity=revision.identity,
                provider_id=coding_cli,
                parent=pipeline,
                candidates=(
                    ()
                    if component_model is None
                    else ((revision.identity, component_model),)
                ),
            )
            flavor_candidates = tuple(
                (
                    (
                        ContentIdentity.parse_uri(flavor.revision_identity)
                        if flavor.revision_identity is not None
                        else canonical_identity(
                            {
                                "flavor_id": flavor.flavor_id,
                                "component_revision": revision.identity.uri,
                            }
                        )
                    ),
                    model,
                )
                for flavor in _flavors_for_node(snapshot, revision.identity)
                if (model := flavor.model_for(coding_cli)) is not None
            )
            try:
                flavor_scope = resolve_model_scope(
                    scope_kind=ModelScopeKind.FLAVOR_ROLE,
                    owner_identity=revision.identity,
                    provider_id=coding_cli,
                    parent=component,
                    candidates=flavor_candidates,
                )
                authoring_identity = revision.authoring_identity

                def read_component_skill(
                    reference: ContentReference,
                    authoring_identity: ContentIdentity = authoring_identity,
                ) -> bytes:
                    return snapshot.component_content(authoring_identity, reference)

                component_skills = _skills(
                    revision.authoring_inputs,
                    read=read_component_skill,
                    source=f"Component {revision.coordinate.uri}",
                )
                selected_skills = (
                    *component_skills,
                    *(
                        skill
                        for flavor in _flavors_for_node(snapshot, revision.identity)
                        for skill in flavor.skills
                    ),
                )
                skill_candidates = tuple(
                    sorted(
                        {
                            (skill.content_identity, model)
                            for skill in selected_skills
                            if (model := skill.model_for(coding_cli)) is not None
                        },
                        key=lambda item: (item[0].uri, item[1]),
                    )
                )
                result[revision.identity.uri] = resolve_model_scope(
                    scope_kind=ModelScopeKind.SKILL_INVOCATION,
                    owner_identity=revision.identity,
                    provider_id=coding_cli,
                    parent=flavor_scope,
                    candidates=skill_candidates,
                )
            except ValueError as exc:
                _error(
                    getattr(exc, "code", "generate.model_conflict"),
                    getattr(
                        exc,
                        "message",
                        f"selected Flavors disagree on the {coding_cli} model",
                    ),
                )
        snapshot.require_unchanged()
        return result

    def identities(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        *,
        coding_cli: str,
    ) -> dict[str, ContentIdentity]:
        return {
            revision: binding.identity
            for revision, binding in self.bindings(
                snapshot, coding_cli=coding_cli
            ).items()
        }


def _toolchains(
    snapshot: LockedGenerationAuthoritySnapshot,
    root_revision: LockedComponentRevision,
) -> tuple[EffectiveToolchainConstraint, ...]:
    sources: list[ResolvedToolchainConstraint] = []
    for reference in root_revision.authoring_inputs:
        if reference.kind == "toolchain-constraint":
            sources.append(
                _toolchain_constraint(
                    reference,
                    content=snapshot.component_content(
                        root_revision.authoring_identity, reference
                    ),
                    source=f"Component {root_revision.coordinate.uri}",
                )
            )
    for flavor in snapshot.authority.selected_flavors:
        if flavor.identity not in root_revision.selected_flavor_revisions:
            continue
        definition = flavor.definition
        for reference in definition.authoring_inputs:
            if reference.kind == "toolchain-constraint":
                sources.append(
                    _toolchain_constraint(
                        reference,
                        content=snapshot.flavor_content(flavor.identity, reference),
                        source=f"Flavor {definition.coordinate.uri}",
                    )
                )
        for contribution in definition.contributions:
            if contribution.kind is ContributionKind.TOOLCHAIN:
                sources.append(
                    _toolchain_constraint(
                        contribution.content,
                        content=snapshot.flavor_content(
                            flavor.identity, contribution.content
                        ),
                        source=(
                            f"Flavor {definition.coordinate.uri} contribution "
                            f"{contribution.contribution_id}"
                        ),
                        contribution=contribution,
                    )
                )
    return _merge_toolchains(sources)


def _require_flavor_slots(
    definition: ComponentDefinition, selected: tuple[RecipeFlavor, ...]
) -> None:
    slots_by_id = {slot.slot_id: slot for slot in definition.flavor_slots}
    counts = {slot_id: 0 for slot_id in slots_by_id}
    for flavor in selected:
        for slot_id in flavor.slot_ids:
            if slot_id not in slots_by_id:
                _error(
                    "generate.flavor_slot_unknown",
                    f"selected Flavor binds unknown Component slot {slot_id!r}",
                )
            counts[slot_id] += 1
    for slot_id, slot in slots_by_id.items():
        count = counts[slot_id]
        minimum = (
            slot.minimum
            if slot.minimum is not None
            else 1
            if slot.cardinality
            in {FlavorCardinality.EXACTLY_ONE, FlavorCardinality.ONE_OR_MORE}
            else 0
        )
        maximum = (
            slot.maximum
            if slot.maximum is not None
            else 1
            if slot.cardinality
            in {FlavorCardinality.EXACTLY_ONE, FlavorCardinality.ZERO_OR_ONE}
            else None
        )
        if count < minimum or (maximum is not None and count > maximum):
            _error(
                "generate.flavor_cardinality",
                f"Flavor slot {slot_id!r} requires {minimum}..{maximum or '*'}; "
                f"selected {count}",
            )


def _contributed_language_entrypoints(
    snapshot: LockedGenerationAuthoritySnapshot,
    selected: tuple[RecipeFlavor, ...],
) -> dict[str, str]:
    flavors = {item.identity.uri: item for item in snapshot.authority.selected_flavors}
    mapping: dict[str, str] = {}
    for recipe_flavor in selected:
        if recipe_flavor.axis != FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value:
            continue
        identity = recipe_flavor.revision_identity
        if identity is None:
            continue
        revision = flavors.get(identity)
        if revision is None:
            continue
        profiles: list[StandardLanguageCommandProfile] = []
        for contribution in revision.definition.contributions:
            if (
                contribution.kind is not ContributionKind.BUILDER
                or contribution.content.kind != STANDARD_COMMAND_PROFILE_CONTENT_KIND
            ):
                continue
            try:
                content = snapshot.flavor_content(
                    revision.identity, contribution.content
                )
                profile = parse_standard_command_profile(
                    json.loads(content.decode("utf-8")),
                    path=(
                        f"Flavor {revision.definition.coordinate.uri} contribution "
                        f"{contribution.contribution_id}"
                    ),
                )
            except (
                ContractValidationError,
                OSError,
                TypeError,
                UnicodeError,
                ValueError,
            ) as exc:
                raise GenerationPreparationError(
                    "generate.language_command_profile_invalid",
                    "a selected language Flavor has an invalid Standard command "
                    "profile",
                ) from exc
            if isinstance(profile, StandardLanguageCommandProfile):
                profiles.append(profile)
        if len(profiles) > 1:
            _error(
                "generate.language_command_profile_ambiguous",
                "a selected language Flavor contributes more than one language "
                "command profile",
            )
        if len(profiles) == 1:
            mapping[identity] = profiles[0].source_entrypoint
    return mapping


def _source_entrypoint_for(
    flavor: RecipeFlavor,
    contributed: dict[str, str],
) -> str:
    identity = flavor.revision_identity
    if identity is not None and identity in contributed:
        return contributed[identity]
    try:
        return portable_source_entrypoint(flavor.value)
    except PortableApplicationError as exc:
        raise GenerationPreparationError(
            "generate.unsupported_implementation_language",
            str(exc),
        ) from exc


def _entrypoints(
    selected: tuple[RecipeFlavor, ...],
    contributed: dict[str, str] | None = None,
    authored_entrypoints: tuple[object, ...] = (),
    *,
    component_kind: str,
) -> tuple[str | None, tuple[str, ...]]:
    if component_kind == "library":
        return None, ()
    languages = tuple(
        item
        for item in selected
        if item.axis == FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value
    )
    if not languages:
        return None, ()
    resolved = contributed or {}
    bindings = tuple((flavor, slot) for flavor in languages for slot in flavor.slot_ids)
    if len(languages) == 1 and len(authored_entrypoints) > 1:
        source_entrypoint = _source_entrypoint_for(languages[0], resolved)
        projected = tuple(
            standard_entrypoint_source_path(
                source_entrypoint,
                getattr(entrypoint, "path", None),
                primary=index == 0,
            )
            for index, entrypoint in enumerate(authored_entrypoints)
        )
        if len(set(projected)) != len(projected):
            _error(
                "generate.entrypoint_source_path_ambiguous",
                "multiple authored entrypoints resolve to the same generated "
                "source path",
            )
        return None, tuple(sorted(projected))
    if len(languages) == 1 and (
        not bindings
        or all(not slot.endswith("-language") for _flavor, slot in bindings)
    ):
        return _source_entrypoint_for(languages[0], resolved), ()
    if not bindings or any(not item.slot_ids for item in languages):
        _error(
            "generate.language_role_binding_missing",
            "multiple implementation languages require exact Component-slot bindings",
        )
    entrypoints: list[str] = []
    for flavor, slot_id in bindings:
        role = (
            slot_id.removesuffix("-language") if slot_id.endswith("-language") else ""
        )
        role_path = PurePosixPath(role)
        if not role or len(role_path.parts) != 1 or role_path.as_posix() != role:
            _error(
                "generate.language_role_entrypoint_unsupported",
                f"language slot {slot_id!r} does not identify one portable role",
            )
        source = PurePosixPath(_source_entrypoint_for(flavor, resolved))
        entrypoints.append(f"source/{role}/{source.name}")
    if len(set(entrypoints)) != len(entrypoints):
        _error(
            "generate.language_role_entrypoint_ambiguous",
            "language role bindings resolve to duplicate generated entrypoints",
        )
    return None, tuple(sorted(entrypoints))


def _library_import_surface_for_recipe(
    authoring: ComponentAuthoring,
    definition: ComponentDefinition,
    node: object,
    selected: tuple[RecipeFlavor, ...],
) -> LibraryImportSurface | None:
    if authoring.resolved_kind != "library":
        return None
    languages = tuple(
        item.value
        for item in selected
        if item.axis == FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value
    )
    if len(languages) != 1:
        _error(
            "generate.library_language_ambiguous",
            "an importable library requires one exact implementation language",
        )
    bindings = {item.capability: item for item in node.interface_bindings}
    missing = tuple(
        item.name for item in definition.provides if item.name not in bindings
    )
    if missing:
        _error(
            "generate.library_interface_required",
            "every library capability requires one exact public interface: "
            + ", ".join(missing),
        )
    try:
        return project_library_import_surface(
            definition.coordinate.name,
            languages[0],
            tuple(
                (item.name, bindings[item.name].interface_identity)
                for item in definition.provides
            ),
            declarations=authoring.library_imports,
        )
    except ValueError as exc:
        _error("generate.library_language_unsupported", str(exc))


def _cpp_library_build_for_recipe(snapshot, authoring, definition, node, selected):
    surface = _library_import_surface_for_recipe(authoring, definition, node, selected)
    if surface is None or surface.language != "cpp":
        return None
    revisions = {
        item.identity.uri: item for item in snapshot.authority.selected_flavors
    }
    profiles = []
    for selected_flavor in selected:
        revision = revisions.get(selected_flavor.revision_identity)
        if revision is None:
            continue
        for contribution in revision.definition.contributions:
            if (
                contribution.kind is ContributionKind.BUILDER
                and contribution.content.kind == STANDARD_COMMAND_PROFILE_CONTENT_KIND
            ):
                profiles.append(
                    parse_standard_command_profile(
                        json.loads(
                            snapshot.flavor_content(
                                revision.identity, contribution.content
                            )
                        )
                    )
                )
    languages = [
        item for item in profiles if isinstance(item, StandardLanguageCommandProfile)
    ]
    platforms = [
        item for item in profiles if isinstance(item, StandardPlatformCommandProfile)
    ]
    builds = [
        item for item in profiles if isinstance(item, StandardBuildSystemCommandProfile)
    ]
    if (
        len(languages) != 1
        or len(platforms) != 1
        or len(builds) != 1
        or languages[0].cpp_library_kind is None
    ):
        _error(
            "generate.cpp_library_profile_required",
            "C++ libraries require exact language, platform and Bazel profiles "
            "and a native product kind",
        )
    return RecipeCppLibraryBuild(
        project_cpp_library_layout(
            surface, kind=languages[0].cpp_library_kind, platform=platforms[0].target
        ),
        builds[0],
        platforms[0],
    )


def _library_dependencies_for_recipe(
    snapshot: LockedGenerationAuthoritySnapshot,
    plan: ComponentGenerationPlan,
    native_sdk_input_identities=None,
) -> tuple[RecipeLibraryDependency, ...]:
    nodes = {item.revision.identity.uri: item for item in snapshot.authority.lock.nodes}
    authorings = {item.identity.uri: item for item in snapshot.authority.authorings}
    consumer_languages = tuple(
        item.value
        for item in _flavors_for_node(snapshot, plan.component_revision)
        if item.axis == FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value
    )
    dependencies = []
    for edge in plan.direct_generation_edges:
        provider = nodes[edge.provider_revision.uri]
        authoring = authorings[provider.revision.authoring_identity.uri]
        definition = _component_definition(
            authoring,
            provider.revision,
            native_sdk_input_identities=(native_sdk_input_identities or {}).get(
                provider.revision.identity.uri, ()
            ),
        )
        if authoring.resolved_kind != "library":
            continue
        if edge.public_interface_identity is None:
            _error(
                "generate.library_interface_required",
                "a direct library dependency requires one exact public interface",
            )
        surface = _library_import_surface_for_recipe(
            authoring,
            definition,
            provider,
            _flavors_for_node(snapshot, provider.revision.identity),
        )
        assert surface is not None
        if consumer_languages != (surface.language,):
            _error(
                "generate.library_cross_language_unsupported",
                "a direct library dependency must use the consumer's exact "
                "implementation language",
            )
        dependencies.append(
            RecipeLibraryDependency(
                edge.provider_revision,
                edge.capability,
                edge.public_interface_identity,
                surface,
            )
        )
    return tuple(
        sorted(
            dependencies,
            key=lambda item: (
                item.provider_component_revision.uri,
                item.capability,
            ),
        )
    )


def _recipe_deployment_units(
    selected: tuple[RecipeFlavor, ...],
    contributed: dict[str, str],
    authored_entrypoints: tuple[Entrypoint, ...],
) -> tuple[RecipeDeploymentUnit, ...]:
    if len(authored_entrypoints) <= 1:
        return ()
    languages = tuple(
        item
        for item in selected
        if item.axis == FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value
    )
    if len(languages) != 1:
        _error(
            "generate.multi_entrypoint_language_ambiguous",
            "multiple authored entrypoints require one exact implementation language",
        )
    source_entrypoint = _source_entrypoint_for(languages[0], contributed)
    units = tuple(
        RecipeDeploymentUnit(
            name=entrypoint.name,
            kind=entrypoint.kind,
            deployment_unit=entrypoint.resolved_deployment_unit,
            source_entrypoint=standard_entrypoint_source_path(
                source_entrypoint,
                entrypoint.path,
                primary=index == 0,
            ),
            entrypoint_identity=canonical_identity(entrypoint.to_dict()).uri,
        )
        for index, entrypoint in enumerate(authored_entrypoints)
    )
    return tuple(sorted(units, key=lambda item: item.deployment_unit))


def _node(snapshot: LockedGenerationAuthoritySnapshot, plan: ComponentGenerationPlan):
    lock = snapshot.authority.lock
    if plan.component_graph_identity != lock.identity:
        _error(
            "component_preparation.lock_mismatch",
            "node generation plan does not bind the exact locked authority",
        )
    matches = tuple(
        item for item in lock.nodes if item.revision.identity == plan.component_revision
    )
    if len(matches) != 1:
        _error(
            "component_preparation.node_missing",
            "node generation plan does not identify one exact locked Component",
        )
    return matches[0]


def _require_node_generation_key(node, plan: ComponentGenerationPlan) -> None:
    revision = node.revision
    key = plan.generation_key
    expected = {
        "specification_set": revision.specification_set_identity,
        "specifications": tuple(item.identity for item in revision.specifications),
        "flavors": tuple(
            sorted(revision.selected_flavor_revisions, key=lambda item: item.uri)
        ),
        "skills": with_debug_skill_identities(
            tuple(
                sorted(
                    (
                        item.identity
                        for item in revision.authoring_inputs
                        if item.kind == "specification-to-source-skill"
                    ),
                    key=lambda item: item.uri,
                )
            )
        ),
        "workflow": revision.workflow_definition.identity,
        "routing": revision.routing_policy.identity,
        "interfaces": tuple(
            sorted(
                (item.identity for item in revision.public_interfaces),
                key=lambda item: item.uri,
            )
        ),
        "assets": tuple(
            sorted(
                (
                    AuthoredBinaryAsset(
                        component_revision=revision.identity,
                        asset_id=item.selector.asset_id,
                        path=item.selector.path,
                        role=item.selector.role,
                        target_identity=node.target_flavor_selection.identity,
                        blob=item.blob,
                        authorization_identity=plan.component_graph_identity,
                    ).identity
                    for item in revision.assets
                ),
                key=lambda item: item.uri,
            )
        ),
    }
    actual = {
        "specification_set": key.specification_set_identity,
        "specifications": key.specification_identities,
        "flavors": key.flavor_identities,
        "skills": key.skill_identities,
        "workflow": key.workflow_identity,
        "routing": key.routing_identity,
        "interfaces": key.exported_public_interface_identities,
        "assets": key.asset_identities,
    }
    mismatches = tuple(name for name in expected if expected[name] != actual[name])
    if mismatches:
        _error(
            "component_preparation.generation_key_mismatch",
            "node generation key differs from locked authority: "
            + ", ".join(mismatches),
        )
    direct = tuple(
        sorted(
            {
                edge.public_interface_identity
                for edge in plan.direct_generation_edges
                if edge.public_interface_identity is not None
            },
            key=lambda item: item.uri,
        )
    )
    if direct != key.direct_public_interface_identities:
        _error(
            "component_preparation.direct_interface_mismatch",
            "node generation key differs from its direct public-interface edges",
        )


def _node_authority_segments(
    snapshot: LockedGenerationAuthoritySnapshot,
    node,
    plan: ComponentGenerationPlan,
) -> tuple[PromptSegmentInput, ...]:
    revision = node.revision
    segments: list[PromptSegmentInput] = []

    def local(kind, reference: ContentReference, reason: str) -> None:
        segments.append(
            PromptSegmentInput(
                kind,
                revision.identity,
                reason,
                reference.identity,
                snapshot.component_content(revision.authoring_identity, reference),
            )
        )

    for reference in revision.specifications:
        local(
            ContextAuthorityKind.LOCAL_SPECIFICATION,
            reference,
            "selected local Component specification",
        )
    selected_flavors = {
        item.identity.uri: item
        for item in snapshot.authority.selected_flavors
        if item.identity in revision.selected_flavor_revisions
    }
    for identity in plan.generation_key.flavor_identities:
        flavor = selected_flavors.get(identity.uri)
        if flavor is None:
            _error(
                "component_preparation.flavor_missing",
                "node selects a Flavor absent from locked generation authority",
            )
        segments.append(
            PromptSegmentInput(
                ContextAuthorityKind.LOCAL_FLAVOR,
                revision.identity,
                "selected local Flavor revision",
                identity,
                canonical_json_bytes(flavor.to_dict()),
            )
        )
    for reference in revision.authoring_inputs:
        if reference.kind == "specification-to-source-skill":
            local(
                ContextAuthorityKind.LOCAL_SKILL,
                reference,
                "selected local specification-to-source skill",
            )
    # Debug spec-map is framework-owned instrumentation injected through
    # the coding-CLI skill closure (coding_cli.py), not through authority
    # segments.  Including it here as LOCAL_SKILL caused
    # generation_context.authority_unselected because the plan does not
    # select diagnostic skills.  See #212.
    local(
        ContextAuthorityKind.LOCAL_WORKFLOW,
        revision.workflow_definition,
        "selected local generation workflow",
    )
    local(
        ContextAuthorityKind.LOCAL_ROUTING_POLICY,
        revision.routing_policy,
        "selected local model routing policy",
    )
    for reference in revision.public_interfaces:
        local(
            ContextAuthorityKind.LOCAL_PUBLIC_INTERFACE,
            reference,
            "exported local public interface",
        )
    for item in revision.assets:
        asset = AuthoredBinaryAsset(
            component_revision=revision.identity,
            asset_id=item.selector.asset_id,
            path=item.selector.path,
            role=item.selector.role,
            target_identity=node.target_flavor_selection.identity,
            blob=item.blob,
            authorization_identity=plan.component_graph_identity,
        )
        segments.append(
            PromptSegmentInput(
                ContextAuthorityKind.LOCAL_ASSET_METADATA,
                revision.identity,
                "locked non-generated asset metadata; bytes are an immutable overlay",
                asset.identity,
                canonical_json_bytes(asset.to_dict()),
            )
        )
    nodes = {item.revision.identity.uri: item for item in snapshot.authority.lock.nodes}
    for edge in plan.direct_generation_edges:
        provider = nodes[edge.provider_revision.uri]
        references = tuple(
            item
            for item in provider.revision.public_interfaces
            if item.identity == edge.public_interface_identity
        )
        if len(references) != 1:
            _error(
                "component_preparation.direct_interface_missing",
                "direct dependency does not expose its exact planned public interface",
            )
        reference = references[0]
        segments.append(
            PromptSegmentInput(
                ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE,
                provider.revision.identity,
                f"direct public interface for requirement {edge.requirement_id}",
                reference.identity,
                snapshot.component_content(
                    provider.revision.authoring_identity, reference
                ),
            )
        )
    return tuple(segments)


class LockedComponentNodePreparationAdapter:
    """Project one planned node from exact selected lock/catalog bytes."""

    def __init__(
        self,
        *,
        model_selector: LockedComponentModelSelectionAdapter | None = None,
        coding_cli: str | None = None,
        execution_documents: tuple[RecipeDocument, ...] = (),
        native_sdk_inputs=None,
    ) -> None:
        if (model_selector is None) != (coding_cli is None):
            raise ValueError("model selector and coding CLI must be supplied together")
        self.model_selector = model_selector
        self.coding_cli = coding_cli
        if any(not isinstance(item, RecipeDocument) for item in execution_documents):
            raise TypeError("execution documents must be RecipeDocument values")
        paths = tuple(item.path for item in execution_documents)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("execution document paths must be uniquely sorted")
        self.execution_documents = execution_documents
        self.native_sdk_inputs = native_sdk_inputs

    @staticmethod
    def authority_lock_identity(
        snapshot: LockedGenerationAuthoritySnapshot,
    ) -> ContentIdentity:
        return snapshot.authority.lock.identity

    def guard(self, snapshot: LockedGenerationAuthoritySnapshot) -> None:
        from literate_ai.adapters.native_sdk_generation import (
            native_sdk_generation_identities,
        )

        snapshot.require_unchanged()
        native_sdk_generation_identities(self.native_sdk_inputs, snapshot)

    def project(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        plan: ComponentGenerationPlan,
    ) -> LockedNodeGenerationProjection[ComponentDefinition, GenerationRecipe]:
        from literate_ai.adapters.native_sdk_generation import (
            bind_native_sdk_generation_inputs,
            native_sdk_generation_identities,
        )

        node = _node(snapshot, plan)
        sdk_identities = native_sdk_generation_identities(
            self.native_sdk_inputs, snapshot
        )
        expected_sdk_ids = (sdk_identities or {}).get(plan.component_revision.uri, ())
        if node.revision.repository_sources and sdk_identities is None:
            _error(
                "generate.repository_source_admission_required",
                "repository source locks require live SDK admission before generation",
            )
        if plan.generation_key.native_sdk_input_identities != expected_sdk_ids:
            _error(
                "component_preparation.native_sdk_input_mismatch",
                "node generation key differs from live admitted SDK inputs",
            )
        debug_skills = with_debug_skill_identities(plan.generation_key.skill_identities)
        if debug_skills != plan.generation_key.skill_identities:
            plan = replace(
                plan,
                generation_key=replace(
                    plan.generation_key, skill_identities=debug_skills
                ),
            )
        _require_node_generation_key(node, plan)
        revision = node.revision
        authoring = next(
            item
            for item in snapshot.authority.authorings
            if item.identity == revision.authoring_identity
        )
        definition = _component_definition(
            authoring, revision, native_sdk_input_identities=expected_sdk_ids
        )
        documents = (
            *_documents_for_node(snapshot, revision.identity),
            *self.execution_documents,
        )
        selected = _flavors_for_node(snapshot, revision.identity)
        _require_flavor_slots(definition, selected)

        def read_component(reference: ContentReference) -> bytes:
            return snapshot.component_content(revision.authoring_identity, reference)

        component_models = _component_models_for_node(snapshot, revision, documents)
        skills = _skills(
            revision.authoring_inputs,
            read=read_component,
            source=f"Component {definition.coordinate.uri}",
        )
        if not skills:
            _error(
                "generate.skill_required",
                "Component source generation requires a pinned "
                "specification-to-source skill",
            )
        contributed_entrypoints = _contributed_language_entrypoints(snapshot, selected)
        authored_entrypoints = tuple(definition.entrypoints)
        singular, plural = _entrypoints(
            selected,
            contributed_entrypoints,
            authored_entrypoints,
            component_kind=authoring.resolved_kind,
        )
        model_scope = (
            None
            if self.model_selector is None or self.coding_cli is None
            else self.model_selector.bindings(snapshot, coding_cli=self.coding_cli)[
                revision.identity.uri
            ]
        )
        if (
            model_scope is not None
            and model_scope.identity != plan.generation_key.model_identity
        ):
            _error(
                "generate.model_scope_mismatch",
                "resolved model scope differs from the planned generation key",
            )
        recipe = GenerationRecipe(
            recipe_id=definition.coordinate.name,
            application_id=definition.coordinate.name,
            documents=documents,
            component_lock_identity=snapshot.authority.lock.identity,
            flavors=selected,
            required_entrypoint=singular,
            models=component_models,
            skills=skills,
            resolved_inputs=(
                ("component_lock", snapshot.authority.lock.identity.uri),
                ("component_revision", revision.identity.uri),
                ("generation_key", plan.generation_key.identity.uri),
                ("model_selection", plan.generation_key.model_identity.uri),
                ("target_flavor_selection", node.target_flavor_selection.identity.uri),
            ),
            required_entrypoints=plural,
            managed_sbom_graph=project_component_lock_managed_graph(
                snapshot.authority.lock,
                revision.identity,
            ),
            model_scope=model_scope,
            skill_catalog=_admitted_skill_catalog(snapshot),
            deployment_units=_recipe_deployment_units(
                selected,
                contributed_entrypoints,
                authored_entrypoints,
            ),
            library_import_surface=_library_import_surface_for_recipe(
                authoring, definition, node, selected
            ),
            cpp_library_build=_cpp_library_build_for_recipe(
                snapshot, authoring, definition, node, selected
            ),
            library_dependencies=_library_dependencies_for_recipe(
                snapshot, plan, sdk_identities
            ),
        )
        segments = _node_authority_segments(snapshot, node, plan)
        if self.native_sdk_inputs is not None:
            recipe = bind_native_sdk_generation_inputs(
                recipe, self.native_sdk_inputs, revision.identity
            )
            segments = (
                *segments,
                *(
                    PromptSegmentInput(
                        ContextAuthorityKind.LOCAL_NATIVE_SDK_METADATA,
                        revision.identity,
                        "direct admitted native SDK consumer input",
                        binding.identity,
                        canonical_json_bytes(binding.to_dict()),
                    )
                    for binding in self.native_sdk_inputs.for_consumer(
                        revision.identity,
                        target_identity=node.target_flavor_selection.identity,
                    )
                ),
            )
            self.guard(snapshot)
        return LockedNodeGenerationProjection(
            revision.identity,
            definition,
            recipe,
            segments,
        )


class FilesystemComponentWorkspaceAllocator:
    """Allocate a distinct empty host directory for each Component attempt."""

    def __init__(self, parent: Path | None = None) -> None:
        self.parent = None if parent is None else parent.resolve(strict=True)
        if self.parent is not None and not self.parent.is_dir():
            raise ValueError("workspace parent must be a directory")

    def allocate(
        self, plan: ComponentGenerationPlan
    ) -> ComponentGenerationWorkspaceDescriptor:
        path = Path(
            tempfile.mkdtemp(
                prefix=f"litai-node-{plan.component_revision.digest[:12]}-",
                dir=self.parent,
            )
        ).resolve(strict=True)
        if any(path.iterdir()):
            raise RuntimeError("new Component workspace is not empty")
        allocation = canonical_identity(
            {
                "schema": "literate-ai/component-workspace-allocation@1",
                "component_revision": plan.component_revision.uri,
                "generation_plan_identity": plan.identity.uri,
                "locator": str(path),
            }
        )
        return ComponentGenerationWorkspaceDescriptor(
            plan.component_revision,
            plan.identity,
            plan.generation_key.identity,
            allocation,
            str(path),
        )


def _nearest_project_root() -> Path | None:
    from literate_ai.projects import discover_project

    loaded = discover_project(Path.cwd())
    return None if loaded is None else loaded.root


class FilesystemLockedGenerationPreparationAdapter:
    """Concrete adapter behind ``GenerationPreparationService``."""

    def __init__(
        self,
        reader: FilesystemLockedGenerationAuthorityReader | None = None,
        *,
        native_sdk_inputs=None,
        native_sdk_builder=None,
    ) -> None:
        self.reader = reader or FilesystemLockedGenerationAuthorityReader()
        self.native_sdk_inputs = native_sdk_inputs
        if native_sdk_inputs is not None and native_sdk_builder is not None:
            raise ValueError("select completed SDK inputs or an SDK builder")
        self.native_sdk_builder = native_sdk_builder

    def load_catalog(self, request: GenerationPreparationRequest):
        return self.reader.load_catalog(
            request.component_root, flavor_roots=request.flavor_roots
        )

    def resolve_lock(self, catalog, request: GenerationPreparationRequest):
        snapshot = self.reader.resolve(
            catalog,
            target_name=request.target_name,
            flavor_selectors=request.flavor_selectors,
        )
        if self.native_sdk_builder is not None:
            self.native_sdk_inputs = self.native_sdk_builder(snapshot)
        return snapshot

    def create_recipe(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        request: GenerationPreparationRequest,
    ) -> PreparedLockedGeneration:
        authority = snapshot.authority
        lock = authority.lock
        root = request.component_root.resolve(strict=True)
        root_node = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        base = root_node.revision
        from literate_ai.adapters.native_sdk_generation import (
            bind_native_sdk_generation_inputs,
            native_sdk_generation_identities,
        )

        sdk_identities = native_sdk_generation_identities(
            self.native_sdk_inputs, snapshot
        )
        definition = _component_definition(
            authority.root_authoring,
            base,
            native_sdk_input_identities=(sdk_identities or {}).get(
                base.identity.uri, ()
            ),
        )
        documents = _documents(snapshot)
        selected = _flavors(snapshot)
        _require_flavor_slots(definition, selected)

        def read_component(reference: ContentReference) -> bytes:
            return snapshot.component_content(base.authoring_identity, reference)

        application: object = next(
            (
                json.loads(document.content)
                for document in documents
                if document.path == "openspec/app.json"
            ),
            {},
        )
        if not isinstance(application, dict):
            _error(
                "generate.invalid_specification",
                "openspec/app.json must contain a JSON object",
            )
        if application.get("schema") == PORTABLE_APPLICATION_SCHEMA:
            application = validate_portable_application(application)
        application_models = _model_mapping(
            application.get("models"), source="application"
        )
        referenced_models = _models(
            base.authoring_inputs,
            read=read_component,
            source=f"Component {definition.coordinate.uri}",
        )
        if application_models and referenced_models:
            _error(
                "generate.invalid_model_selection",
                "Component has multiple coding-model specifications",
            )
        skills = _skills(
            base.authoring_inputs,
            read=read_component,
            source=f"Component {definition.coordinate.uri}",
        )
        if not skills:
            _error(
                "generate.skill_required",
                "Component source generation requires a pinned "
                "specification-to-source skill",
            )
        contributed_entrypoints = _contributed_language_entrypoints(snapshot, selected)
        authored_entrypoints = tuple(definition.entrypoints)
        singular, plural = _entrypoints(
            selected,
            contributed_entrypoints,
            authored_entrypoints,
            component_kind=authority.root_authoring.resolved_kind,
        )
        recipe = GenerationRecipe(
            recipe_id=request.recipe_id or definition.coordinate.name,
            application_id=definition.coordinate.name,
            documents=documents,
            component_lock_identity=lock.identity,
            flavors=selected,
            required_entrypoint=singular,
            models=referenced_models or application_models,
            skills=skills,
            resolved_inputs=(
                ("component_lock", lock.identity.uri),
                ("root_revision", lock.root_revision.uri),
                ("target_profile", lock.target_profile_identity.uri),
                ("selection_policy", lock.selection_policy_identity.uri),
            ),
            required_entrypoints=plural,
            managed_sbom_graph=CycloneDxManagedGraph.from_component_lock(lock),
            skill_catalog=_admitted_skill_catalog(snapshot),
            deployment_units=_recipe_deployment_units(
                selected,
                contributed_entrypoints,
                authored_entrypoints,
            ),
            library_import_surface=_library_import_surface_for_recipe(
                authority.root_authoring, definition, root_node, selected
            ),
            cpp_library_build=_cpp_library_build_for_recipe(
                snapshot, authority.root_authoring, definition, root_node, selected
            ),
        )
        if self.native_sdk_inputs is not None:
            recipe = bind_native_sdk_generation_inputs(
                recipe, self.native_sdk_inputs, base.identity
            )
            self.guard(snapshot)
        return PreparedLockedGeneration(
            root,
            project_boundary(root, legacy=root),
            definition,
            recipe,
            selected,
            base,
            _toolchains(snapshot, base),
            snapshot.input_closure,
            snapshot.catalog_audit_identity,
            snapshot,
            self.native_sdk_inputs,
        )

    def guard(self, snapshot: LockedGenerationAuthoritySnapshot) -> None:
        from literate_ai.adapters.native_sdk_generation import (
            native_sdk_generation_identities,
        )

        snapshot.require_unchanged()
        native_sdk_generation_identities(self.native_sdk_inputs, snapshot)


class GenerationPlanningInput(Protocol):
    definition: ComponentDefinition
    recipe: GenerationRecipe


class GenerationAuthorityReviewInput(Protocol):
    component_root: Path


class GenerationExecutionPlanningAdapter:
    """Compile the exact workflow and routing plan from locked authority."""

    def plan(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        prepared: PreparedLockedGeneration,
        *,
        selection: CodingCliSelection | None = None,
        model: str | None = None,
    ) -> GenerationExecutionPlan:
        root_node = next(
            item
            for item in snapshot.authority.lock.nodes
            if item.revision.identity == snapshot.authority.lock.root_revision
        )
        return self._compile(
            prepared,
            workflow=snapshot.component_content(
                root_node.revision.authoring_identity,
                root_node.revision.workflow_definition,
            ),
            routing=snapshot.component_content(
                root_node.revision.authoring_identity,
                root_node.revision.routing_policy,
            ),
            selection=selection,
            model=model,
        )

    def plan_node(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        prepared: PreparedComponentGenerationNode[
            ComponentDefinition,
            GenerationRecipe,
        ],
        *,
        selection: CodingCliSelection | None = None,
        model: str | None = None,
    ) -> GenerationExecutionPlan:
        """Compile one prepared node against that node's exact locked authority."""

        if not isinstance(prepared, PreparedComponentGenerationNode):
            raise TypeError("prepared must be a PreparedComponentGenerationNode")
        locked_node = next(
            (
                item
                for item in snapshot.authority.lock.nodes
                if item.revision.identity == prepared.plan.component_revision
            ),
            None,
        )
        if locked_node is None:
            _error(
                "plan.component_revision_missing",
                "prepared Component revision is absent from locked authority",
            )
        return self._compile(
            prepared,
            workflow=snapshot.component_content(
                locked_node.revision.authoring_identity,
                locked_node.revision.workflow_definition,
            ),
            routing=snapshot.component_content(
                locked_node.revision.authoring_identity,
                locked_node.revision.routing_policy,
            ),
            selection=selection,
            model=model,
        )

    def plan_unlocked(
        self,
        prepared: GenerationPlanningInput,
        *,
        reference_reader: Callable[[ContentReference, str], bytes],
        selection: CodingCliSelection | None = None,
        model: str | None = None,
    ) -> GenerationExecutionPlan:
        """Compile legacy authoring validation through an injected file reader."""

        return self._compile(
            prepared,
            workflow=reference_reader(
                prepared.definition.workflow_definition, "workflow"
            ),
            routing=reference_reader(
                prepared.definition.routing_policy, "routing_policy"
            ),
            selection=selection,
            model=model,
        )

    @staticmethod
    def _compile(
        prepared: GenerationPlanningInput,
        *,
        workflow: bytes,
        routing: bytes,
        selection: CodingCliSelection | None,
        model: str | None,
    ) -> GenerationExecutionPlan:
        endpoint_name = selection.name if selection is not None else "validation"
        # Bind only the portable tool identity. The executable digest is already
        # closed over by tool_binding_identity and cannot be reconstructed by
        # AcceptedSourceCodingCliSelection during from-accepted-source continuation;
        # embedding it here made execution_plan_identity differ across isolated
        # admit-then-continue projects (#139).
        endpoint_url = (
            "cli://validation"
            if selection is None
            else (
                f"cli://{selection.name}/tool"
                f"?binding={quote(selection.tool_binding_identity, safe='')}"
            )
        )
        endpoint = ModelEndpoint(
            f"coding-cli-{endpoint_name}",
            f"coding-cli/{endpoint_name}",
            source_cache_model_selector(model, path="GenerationExecutionPlan.model"),
            endpoint_url,
            Locality.UNKNOWN,
            ("structured-output", "source-generation"),
            1_000_000,
            model_revision=None,
        )
        plan = compile_generation_execution_plan(
            component=prepared.definition,
            workflow_content=workflow,
            routing_content=routing,
            endpoint=endpoint,
            generation_prompt=prepared.recipe.prompt(),
            project_root=_nearest_project_root(),
        )
        stage_ids = {item.stage_id for item in plan.model_stages}
        if any(
            not set(skill.stages).issubset(stage_ids)
            for skill in prepared.recipe.resolved_skills
        ):
            _error(
                "plan.skill_stage_unavailable",
                "a specification-to-source skill targets a stage outside the workflow",
            )
        return plan


class GenerationAuthorityReviewAdapter:
    """Require project review when readable Component authority is present."""

    def __init__(
        self,
        validator: Callable[[Path], ContentIdentity],
    ) -> None:
        self.validator = validator

    def review(
        self,
        _authority: object,
        prepared: GenerationAuthorityReviewInput,
    ) -> ContentIdentity | None:
        authoring = prepared.component_root / "component.md"
        if not authoring.exists() and not authoring.is_symlink():
            return None
        from literate_ai.projects import discover_project

        project = discover_project(prepared.component_root)
        if project is None:
            _error(
                "generate.component_authority_review_required",
                "component.md generation requires a canonical project authority review",
            )
        return self.validator(project.root)


class FilesystemLockedGenerationApplicationAdapter:
    """Compose the public locked-generation services for filesystem clients.

    CLI modules should construct requests and present results; this adapter owns the
    application-service wiring and the repeated authority-drift checks around planning
    and review.
    """

    def __init__(
        self,
        preparation: FilesystemLockedGenerationPreparationAdapter | None = None,
        planner: GenerationExecutionPlanningAdapter | None = None,
    ) -> None:
        self.preparation = preparation or FilesystemLockedGenerationPreparationAdapter()
        self.planner = planner or GenerationExecutionPlanningAdapter()

    def prepare(
        self, request: GenerationPreparationRequest
    ) -> PreparedLockedGeneration:
        service = GenerationPreparationService(
            catalog_loader=self.preparation.load_catalog,
            lock_resolver=self.preparation.resolve_lock,
            recipe_creator=self.preparation.create_recipe,
            authority_guard=self.preparation.guard,
        )
        prepared = service.prepare(request).recipe
        self.require_unchanged(prepared)
        return prepared

    def plan(
        self,
        prepared: PreparedLockedGeneration,
        *,
        selection: CodingCliSelection | None = None,
        model: str | None = None,
    ) -> GenerationExecutionPlan:
        self.require_unchanged(prepared)
        service = GenerationPreparationService(
            execution_planner=lambda snapshot, recipe: self.planner.plan(
                snapshot,
                recipe,
                selection=selection,
                model=model,
            ),
            authority_guard=lambda snapshot: snapshot.require_unchanged(),
        )
        result = service.plan(prepared.locked_authority_snapshot, prepared)
        self.require_unchanged(prepared)
        return result

    def review(
        self,
        prepared: PreparedLockedGeneration,
        validator: Callable[[Path], ContentIdentity],
    ) -> ContentIdentity | None:
        self.require_unchanged(prepared)
        reviewer = GenerationAuthorityReviewAdapter(validator)
        service = GenerationPreparationService(
            authority_reviewer=reviewer.review,
            authority_guard=lambda snapshot: snapshot.require_unchanged(),
        )
        result = service.review(prepared.locked_authority_snapshot, prepared)
        self.require_unchanged(prepared)
        return result

    @staticmethod
    def require_unchanged(
        prepared: PreparedLockedGeneration,
    ) -> PinnedInputClosure:
        if not isinstance(prepared, PreparedLockedGeneration):
            raise TypeError("prepared must be a PreparedLockedGeneration")
        prepared.locked_authority_snapshot.require_unchanged()
        prepared.input_closure.require_unchanged()
        if prepared.native_sdk_inputs is not None:
            from literate_ai.adapters.native_sdk_generation import (
                native_sdk_generation_identities,
            )

            native_sdk_generation_identities(
                prepared.native_sdk_inputs, prepared.locked_authority_snapshot
            )
        return prepared.input_closure


__all__ = [
    "EffectiveToolchainConstraint",
    "FilesystemLockedGenerationApplicationAdapter",
    "FilesystemLockedGenerationPreparationAdapter",
    "GenerationAuthorityReviewAdapter",
    "GenerationExecutionPlanningAdapter",
    "LockedComponentModelSelectionAdapter",
    "PreparedLockedGeneration",
    "ResolvedToolchainConstraint",
]
