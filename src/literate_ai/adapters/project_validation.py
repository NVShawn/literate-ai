"""Filesystem adapter for canonical project validation and authority review."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, NoReturn

from literate_ai.adapters.component_markdown import (
    ComponentMarkdownError,
    parse_component_markdown,
)
from literate_ai.adapters.legacy_generation_catalog import load_flavor_catalog
from literate_ai.adapters.project_mcp import (
    ProjectMcpError,
    assert_project_mcp_hygiene,
)
from literate_ai.adapters.repository_lineage import (
    FilesystemRepositoryLineageStore,
    RepositoryLineageStoreError,
)
from literate_ai.application.planning import (
    GenerationPlanningError,
    normalize_generation_workflow_document,
)
from literate_ai.application.project_authority import (
    AuthorityReviewDocument,
    CatalogAuthorityReviewEntry,
    ComponentAuthorityReviewEntry,
    FlavorAuthorityReviewEntry,
    ForwardSkillAuthorityReviewEntry,
    InverseSkillAuthorityReviewEntry,
    ProjectAuthorityError,
    ProjectAuthorityInventory,
    ProjectAuthorityReview,
    documentation_path_is_execution_queue,
    review_project_authority,
)
from literate_ai.authority_graph import AuthorityGraphError
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    HashAlgorithm,
    ResolvedSpecificationToSourceSkill,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    SourceIntelligenceStageStatus,
)
from literate_ai.documentation import (
    DocumentationError,
    validate_documentation_graph,
)
from literate_ai.project_authority_graph import project_authority_graph
from literate_ai.project_source_index import (
    CodeGraphProjectSourceIntelligence,
    ProjectSourceIntelligenceError,
)
from literate_ai.projects import (
    DEFAULT_MAXIMUM_DOCUMENTATION_FILE_BYTES,
    DEFAULT_MAXIMUM_DOCUMENTATION_FILES,
    DEFAULT_MAXIMUM_DOCUMENTATION_TOTAL_BYTES,
    PROJECT_FILENAME,
    PinnedInputClosure,
    PinnedInputClosureError,
    ProjectError,
    discover_project,
    documentation_files,
    source_to_specification_skill_paths,
    specification_to_source_skill_paths,
    validate_project_structure,
)
from literate_ai.source_to_specification import (
    SourceToSpecificationError,
    SpecAuthoringSkillSet,
    canonical_value,
    load_skill_manifest,
    resolve_skill_set,
)
from literate_ai.test_receipts import inspect_project_test_receipt

PROJECT_VALIDATION_SCHEMA = "literate-ai/project-validation@6"

_DOC_IDENTITY_MARKER = "DOC-IDENTITY:"


class ProjectValidationError(ValueError):
    """Stable filesystem-project validation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _error(error: BaseException, fallback_code: str, fallback_message: str) -> NoReturn:
    message = getattr(error, "message", None)
    if not isinstance(message, str) or not message:
        message = str(error) or fallback_message
    raise ProjectValidationError(
        str(getattr(error, "code", fallback_code)),
        message,
    ) from error


def _documentation_catalog(project: Any) -> dict[str, object]:
    try:
        paths = documentation_files(project)
        closure = PinnedInputClosure(
            maximum_files=DEFAULT_MAXIMUM_DOCUMENTATION_FILES + 1,
            maximum_file_bytes=DEFAULT_MAXIMUM_DOCUMENTATION_FILE_BYTES,
            maximum_total_bytes=(
                DEFAULT_MAXIMUM_DOCUMENTATION_TOTAL_BYTES
                + DEFAULT_MAXIMUM_DOCUMENTATION_FILE_BYTES
            ),
        )
        skill_content = closure.pin(
            project.agent_skill,
            boundary=project.root,
            label="documentation:SKILL.md",
        )
        captured = {
            path.relative_to(project.root).as_posix(): closure.pin(
                path,
                boundary=project.root,
                label="documentation:" + path.relative_to(project.root).as_posix(),
            )
            for path in paths
        }
    except (ProjectError, PinnedInputClosureError) as exc:
        _error(exc, "project.documentation_invalid", "project documentation is invalid")
    documents = {
        path: content
        for path, content in captured.items()
        if Path(path).suffix.casefold() == ".md"
    }
    assets = {
        path: content
        for path, content in captured.items()
        if Path(path).suffix.casefold() != ".md"
    }
    try:
        graph = validate_documentation_graph(
            project_root=project.root,
            agent_skill=project.agent_skill,
            agent_skill_content=skill_content,
            documents=documents,
            assets=assets,
        )
        closure.require_unchanged()
    except (DocumentationError, PinnedInputClosureError) as exc:
        _error(exc, "project.documentation_invalid", "project documentation is invalid")
    document_report: list[dict[str, str]] = []
    for path, content in sorted(documents.items()):
        try:
            content.decode("utf-8")
        except UnicodeError as exc:
            raise ProjectValidationError(
                "project.documentation_invalid",
                "declared Markdown document is unreadable UTF-8",
            ) from exc
        document_report.append(
            {"path": path, "identity": "sha256:" + hashlib.sha256(content).hexdigest()}
        )
    return {
        "documents": tuple(document_report),
        "assets": tuple(graph["assets"]),
        "graph": graph,
        "skill_content": skill_content,
        "document_contents": documents,
        "asset_contents": assets,
    }


def _agent_skill(project: Any, catalog: dict[str, object]) -> dict[str, object]:
    try:
        raw = catalog["skill_content"]
        assert isinstance(raw, bytes)
        content = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ProjectValidationError(
            "project.agent_skill_invalid", "agent onboarding skill is unreadable"
        ) from exc
    lines = content.splitlines()
    frontmatter_end = lines[1:].index("---") + 1 if "---" in lines[1:] else 0
    if (
        not lines
        or lines[0] != "---"
        or frontmatter_end == 0
        or "name: literate-ai" not in lines[1:frontmatter_end]
    ):
        raise ProjectValidationError(
            "project.agent_skill_invalid",
            "agent onboarding skill requires Literate AI skill frontmatter",
        )
    graph = catalog["graph"]
    assert isinstance(graph, dict)
    links = graph["links"]
    assert isinstance(links, list)
    linked = sorted(
        {
            str(item["target"])
            for item in links
            if isinstance(item, dict)
            and item.get("source") == project.definition.agent_skill
            and isinstance(item.get("target"), str)
            and str(item["target"]).casefold().endswith(".md")
        }
    )
    return {
        "path": project.definition.agent_skill,
        "identity": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "documentation_links": linked,
    }


def _skill_catalog(project: Any) -> tuple[ResolvedSpecificationToSourceSkill, ...]:
    loaded: list[ResolvedSpecificationToSourceSkill] = []
    try:
        catalog_paths = specification_to_source_skill_paths(
            project, validate_catalog=False
        )
    except ProjectError as exc:
        _error(exc, "project.skill_invalid", "project skill catalog is invalid")
    for path in catalog_paths:
        reference_uri = path.relative_to(project.root).as_posix()
        reference: ContentReference | None = None
        try:
            content = path.read_bytes()
            identity = ContentIdentity(
                HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()
            )
            reference = ContentReference(
                "specification-to-source-skill",
                reference_uri,
                identity,
            )
            loaded.append(
                ResolvedSpecificationToSourceSkill.from_reference(
                    reference,
                    content,
                    source=f"project catalog {reference.uri}",
                )
            )
        except (OSError, UnicodeError, ValueError) as exc:
            selected = (
                f"specification-to-source-skill:{reference_uri}"
                if reference is None
                else reference.uri
            )
            raise ProjectValidationError(
                "project.skill_invalid",
                f"invalid specification-to-source skill: {selected}",
            ) from exc
    by_id = {item.skill_id: item for item in loaded}
    if len(by_id) != len(loaded):
        raise ProjectValidationError(
            "project.skill_duplicate", "skill IDs must be unique across the project"
        )
    for skill in loaded:
        for dependency in skill.dependencies:
            selected = by_id.get(dependency.skill_id)
            if selected is None:
                raise ProjectValidationError(
                    "project.skill_dependency_missing",
                    f"skill {skill.skill_id} has a missing dependency",
                )
            if selected.ref != dependency:
                raise ProjectValidationError(
                    "project.skill_dependency_identity_mismatch",
                    f"skill {skill.skill_id} dependency identity changed",
                )
    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(skill_id: str) -> None:
        if skill_id in visiting:
            raise ProjectValidationError(
                "project.skill_dependency_cycle", "skill dependency graph has a cycle"
            )
        if skill_id in complete:
            return
        visiting.add(skill_id)
        for dependency in by_id[skill_id].dependency_ids:
            visit(dependency)
        visiting.remove(skill_id)
        complete.add(skill_id)

    for skill_id in sorted(by_id):
        visit(skill_id)
    return tuple(sorted(loaded, key=lambda item: item.skill_id))


def _inverse_skill_catalog(project: Any) -> tuple[dict[str, object], ...]:
    try:
        catalog_paths = source_to_specification_skill_paths(
            project, validate_catalog=False
        )
    except ProjectError as exc:
        _error(
            exc,
            "project.source_to_specification_skill_invalid",
            "project source-to-specification skill catalog is invalid",
        )
    by_id: dict[str, tuple[Any, Path]] = {}
    for path in catalog_paths:
        try:
            skill = load_skill_manifest(path)
        except SourceToSpecificationError as exc:
            raise ProjectValidationError(
                "project.source_to_specification_skill_invalid",
                "invalid source-to-specification skill: "
                + path.relative_to(project.root).as_posix(),
            ) from exc
        if skill.skill_id in by_id:
            raise ProjectValidationError(
                "project.source_to_specification_skill_duplicate",
                "source-to-specification skill IDs must be unambiguous across "
                f"declared roots: {skill.skill_id}",
            )
        by_id[skill.skill_id] = (skill, path)
    for skill, _path in by_id.values():
        missing = set(skill.dependencies) - by_id.keys()
        if missing:
            raise ProjectValidationError(
                "project.source_to_specification_skill_dependency_missing",
                f"source-to-specification skill {skill.skill_id} requires: "
                + ", ".join(sorted(missing)),
            )
    visiting: set[str] = set()
    complete: set[str] = set()
    ordered: list[str] = []

    def visit(skill_id: str) -> None:
        if skill_id in visiting:
            raise ProjectValidationError(
                "project.source_to_specification_skill_order_cycle",
                "source-to-specification dependency/order graph has a cycle",
            )
        if skill_id in complete:
            return
        visiting.add(skill_id)
        skill = by_id[skill_id][0]
        prerequisites = set(skill.dependencies) | (set(skill.after) & by_id.keys())
        for prerequisite in sorted(prerequisites):
            visit(prerequisite)
        visiting.remove(skill_id)
        complete.add(skill_id)
        ordered.append(skill_id)

    for skill_id in sorted(by_id):
        visit(skill_id)
    if ordered:
        selected = SpecAuthoringSkillSet(
            skill_set_id="project-validation",
            version="1.0.0",
            skills=tuple(by_id[skill_id][0].ref for skill_id in ordered),
        )
        try:
            resolve_skill_set(selected, {key: pair[0] for key, pair in by_id.items()})
        except SourceToSpecificationError as exc:
            raise ProjectValidationError(
                "project.source_to_specification_skill_order_invalid",
                "source-to-specification skill order is invalid",
            ) from exc
    report: list[dict[str, object]] = []
    for skill_id in ordered:
        skill, path = by_id[skill_id]
        value = canonical_value(skill)
        assert isinstance(value, dict)
        report.append(
            {
                "schema": skill.schema,
                "source": path.relative_to(project.root).as_posix(),
                **value,
            }
        )
    return tuple(report)


def _catalog_file_identities(
    project: Any, catalog: str
) -> tuple[dict[str, object], ...]:
    closure = PinnedInputClosure()
    report: list[dict[str, object]] = []
    authorities: dict[tuple[Path, Path], str] = {}
    try:
        for root in project.roots(catalog):
            for configured in sorted(root.rglob("*")):
                if configured.is_symlink():
                    raise ProjectError(
                        f"project.{catalog}_invalid",
                        f"declared {catalog} catalogs cannot contain symbolic links",
                    )
                if configured.is_dir():
                    continue
                if not configured.is_file():
                    raise ProjectError(
                        f"project.{catalog}_invalid",
                        f"declared {catalog} entries must be regular files",
                    )
                relative = configured.relative_to(project.root).as_posix()
                content = closure.pin(
                    configured,
                    boundary=project.root,
                    label=f"{catalog}:{relative}",
                )
                if catalog == "workflow":
                    if configured.suffix not in {".md", ".json"}:
                        raise ProjectError(
                            "project.workflow_invalid",
                            "generation workflow authority must be workflow.md or "
                            "a legacy JSON migration input",
                        )
                    authority_key = (root, configured.relative_to(root).with_suffix(""))
                    if authority_key in authorities:
                        raise ProjectError(
                            "project.workflow_authority_ambiguous",
                            "workflow catalog contains both Markdown and JSON "
                            "authority "
                            f"for {authority_key[1].as_posix()}",
                        )
                    authorities[authority_key] = configured.suffix
                    try:
                        normalize_generation_workflow_document(
                            content,
                            project_root=project.root,
                            source=configured,
                        )
                    except GenerationPlanningError as exc:
                        raise ProjectError(
                            "project.workflow_invalid",
                            f"generation workflow is invalid: {relative}",
                        ) from exc
                report.append(
                    {
                        "path": relative,
                        "identity": "sha256:" + hashlib.sha256(content).hexdigest(),
                    }
                )
        closure.require_unchanged()
    except (ProjectError, PinnedInputClosureError) as exc:
        _error(exc, f"project.{catalog}_invalid", f"project {catalog} is invalid")
    return tuple(report)


def _validate_component_authoring(manifest: Path, project: Any) -> dict[str, object]:
    closure = PinnedInputClosure()
    try:
        content = closure.pin(
            manifest,
            boundary=project.root,
            label="component-authoring:"
            + manifest.relative_to(project.root).as_posix(),
        )
        authoring = parse_component_markdown(
            manifest, content.decode("utf-8"), project_root=project.root
        )
        global_kinds = {
            "model-selection",
            "routing-policy",
            "specification-to-source-skill",
            "toolchain-constraint",
            "workflow",
        }
        generation_skills = tuple(
            item
            for item in authoring.authoring_inputs
            if item.kind == "specification-to-source-skill"
        )
        if not generation_skills:
            raise ProjectValidationError(
                "generate.skill_required",
                "Component source generation requires a specification-to-source skill",
            )
        try:
            admitted_skills = frozenset(specification_to_source_skill_paths(project))
        except ProjectError as exc:
            _error(exc, "project.skill_invalid", "project skill catalog is invalid")
        for selector in generation_skills:
            selected = project.root.joinpath(*Path(selector.uri).parts).resolve()
            if selected not in admitted_skills:
                raise ProjectValidationError(
                    "generate.skill_not_cataloged",
                    "Component specification-to-source skill is outside the declared "
                    "project skill catalog",
                )

        def resolve(selector: Any, label: str) -> ContentIdentity:
            base = project.root if selector.kind in global_kinds else manifest.parent
            path = base.joinpath(*Path(selector.uri).parts)
            selected = closure.pin(
                path,
                boundary=project.root,
                label=f"component-authoring:{authoring.identity.digest}:{label}",
                expected_identity=selector.pin,
            )
            return ContentIdentity(
                HashAlgorithm.SHA256, hashlib.sha256(selected).hexdigest()
            )

        workflow_identity = resolve(authoring.workflow_definition, "workflow")
        routing_identity = resolve(authoring.routing_policy, "routing")
        for index, uri in enumerate(authoring.specification_roots):
            closure.pin(
                manifest.parent.joinpath(*Path(uri).parts),
                boundary=project.root,
                label=f"component-authoring:{authoring.identity.digest}:spec:{index}",
            )
        selectors = (
            *authoring.authoring_inputs,
            *authoring.acceptance_contracts,
            *(
                item.interface
                for item in authoring.provides
                if item.interface is not None
            ),
            *(
                item.integration_contract
                for item in authoring.source_dependencies
                if item.integration_contract is not None
            ),
        )
        for index, selector in enumerate(selectors):
            resolve(selector, f"selector:{index}")
        closure.require_unchanged()
        _validate_sample_taxonomy(manifest, authoring, project)
    except ProjectValidationError:
        raise
    except (
        ComponentMarkdownError,
        PinnedInputClosureError,
        UnicodeError,
        OSError,
        ValueError,
    ) as exc:
        _error(exc, "project.component_invalid", "Component authoring is invalid")
    return {
        "coordinate": authoring.coordinate.uri,
        "revision_identity": authoring.identity.uri,
        "workflow_identity": workflow_identity.uri,
        "routing_identity": routing_identity.uri,
        "input_closure_identity": closure.identity,
        "authority_convergence_identity": None,
        "authoring_format": "component.md",
    }


def _validate_sample_taxonomy(manifest: Path, authoring: Any, project: Any) -> None:
    """Keep demos distinct from reusable Component authority."""

    try:
        relative = manifest.relative_to(project.root)
    except ValueError:
        return
    if not relative.parts or relative.parts[0] != "samples":
        return
    if not authoring.sample:
        raise ProjectValidationError(
            "project.sample_taxonomy_invalid",
            "reusable Components must live in components/, not samples/: "
            + relative.as_posix(),
        )
    expected_inheritable = authoring.coordinate.name == "hello-component"
    if authoring.inheritable != expected_inheritable:
        rule = (
            "the hello-component sample must remain inheritable"
            if expected_inheritable
            else "samples must declare inheritable: false"
        )
        raise ProjectValidationError(
            "project.sample_inheritance_invalid",
            f"{rule}: {relative.as_posix()}",
        )


def _authority_review(
    *,
    project: Any,
    repository_lineage: ContentIdentity,
    onboarding: dict[str, object],
    documentation: dict[str, object],
    components: list[dict[str, object]],
    flavors: tuple[Any, ...],
    skills: tuple[ResolvedSpecificationToSourceSkill, ...],
    inverse_skills: tuple[dict[str, object], ...],
    workflows: tuple[dict[str, object], ...],
    routing: tuple[dict[str, object], ...],
    required: bool,
) -> ProjectAuthorityReview:
    raw_documents = documentation["document_contents"]
    raw_assets = documentation["asset_contents"]
    assert isinstance(raw_documents, dict) and isinstance(raw_assets, dict)
    inventory = ProjectAuthorityInventory(
        project_definition=project.definition.identity,
        repository_lineage=repository_lineage,
        onboarding_skill=ContentIdentity.parse_uri(str(onboarding["identity"])),
        components=tuple(
            ComponentAuthorityReviewEntry(
                coordinate=str(item["coordinate"]),
                revision=str(item["revision_identity"]),
                workflow=str(item["workflow_identity"]),
                routing=str(item["routing_identity"]),
                input_closure=str(item["input_closure_identity"]),
                authority_convergence=(
                    None
                    if item.get("authority_convergence_identity") is None
                    else str(item["authority_convergence_identity"])
                ),
            )
            for item in components
        ),
        flavors=tuple(
            FlavorAuthorityReviewEntry(
                item.coordinate_uri,
                item.revision_identity,
                item.specification_set_identity,
            )
            for item in flavors
        ),
        specification_to_source_skills=tuple(
            ForwardSkillAuthorityReviewEntry(item.skill_id, item.version, item.identity)
            for item in skills
        ),
        source_to_specification_skills=tuple(
            InverseSkillAuthorityReviewEntry(
                str(item["skill_id"]),
                str(item["version"]),
                str(item["content_digest"]),
            )
            for item in inverse_skills
        ),
        workflows=tuple(
            CatalogAuthorityReviewEntry(str(item["path"]), str(item["identity"]))
            for item in workflows
        ),
        routing=tuple(
            CatalogAuthorityReviewEntry(str(item["path"]), str(item["identity"]))
            for item in routing
        ),
        documentation=tuple(
            AuthorityReviewDocument(str(path), content)
            for path, content in raw_documents.items()
            if not documentation_path_is_execution_queue(str(path))
        ),
        documentation_assets=tuple(
            AuthorityReviewDocument(str(path), content)
            for path, content in raw_assets.items()
            if not documentation_path_is_execution_queue(str(path))
        ),
    )
    try:
        return review_project_authority(inventory, required=required)
    except ProjectAuthorityError as exc:
        raise ProjectValidationError(exc.code, exc.message) from exc


def _doc_identity_advisories(project) -> list[dict[str, str]]:
    """Detect documentation files that still contain the template placeholder marker.

    Returns a list of advisory dicts for each file whose content includes the
    ``DOC-IDENTITY:`` HTML comment, meaning the project has not yet replaced the
    init-template placeholders with its own identity.
    """
    advisories: list[dict[str, str]] = []
    for root in project.roots("documentation"):
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _DOC_IDENTITY_MARKER in text:
                relative = path.relative_to(project.root).as_posix()
                advisories.append(
                    {
                        "code": "project.doc_identity_placeholder",
                        "path": relative,
                        "message": (
                            f"{relative} still contains the DOC-IDENTITY template "
                            "placeholder; replace it with this project's own "
                            "description before release"
                        ),
                    }
                )
    return advisories


class FilesystemProjectValidationAdapter:
    """Validate one project using only declared, content-pinned filesystem inputs."""

    def validate(
        self,
        selected: Path,
        *,
        require_authority_review: bool,
        include_test_receipt: bool = True,
        synchronize_source_intelligence: bool = True,
        source_intelligence_stage: SourceIntelligenceStage = (
            SourceIntelligenceStage.PROJECT_MAINTENANCE
        ),
        require_monorepo_current: bool = True,
    ) -> dict[str, Any]:
        try:
            project = discover_project(selected)
        except ProjectError as exc:
            _error(exc, "project.invalid", "project discovery failed")
        if project is None:
            raise ProjectValidationError(
                "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
            )
        try:
            layout = validate_project_structure(project)
        except ProjectError as exc:
            _error(exc, "project.invalid", "project structure is invalid")
        monorepo_components: dict[str, object] | None = None
        monorepo_installation = (
            project.root / ".literate" / "monorepo-components" / "installation.json"
        )
        if monorepo_installation.exists() or monorepo_installation.is_symlink():
            from literate_ai.adapters.monorepo_adoption import MonorepoAdoptionError
            from literate_ai.adapters.monorepo_components import (
                check_installed_monorepo_components,
            )

            try:
                monorepo_components = check_installed_monorepo_components(project.root)
            except MonorepoAdoptionError as exc:
                if require_monorepo_current:
                    raise ProjectValidationError(exc.code, exc.message) from exc
                monorepo_components = {
                    "state": "stale",
                    "error": {"code": exc.code, "message": exc.message},
                }
        try:
            assert_project_mcp_hygiene(
                mcp_roots=project.roots("mcp"),
                skill_roots=project.roots("skill"),
            )
        except ProjectMcpError as exc:
            raise ProjectValidationError(exc.code, exc.message) from exc
        try:
            parent_selection, repository_lineage = FilesystemRepositoryLineageStore(
                project.root
            ).load()
        except RepositoryLineageStoreError as exc:
            raise ProjectValidationError(exc.code, exc.message) from exc
        documentation = _documentation_catalog(project)
        onboarding = _agent_skill(project, documentation)
        skills = _skill_catalog(project)
        inverse_skills = _inverse_skill_catalog(project)
        workflows = _catalog_file_identities(project, "workflow")
        routing = _catalog_file_identities(project, "routing")
        try:
            flavors = load_flavor_catalog(project.roots("flavor"))
        except Exception as exc:
            _error(exc, "generate.invalid_flavor", "Flavor catalog is invalid")
        components: list[dict[str, object]] = []
        for root in project.roots("component"):
            authored_directories: set[Path] = set()
            for manifest in sorted(root.rglob("component.md")):
                if manifest.is_symlink() or not manifest.is_file():
                    raise ProjectValidationError(
                        "project.component_invalid",
                        "Component authoring documents must be regular files",
                    )
                authored_directories.add(manifest.parent)
                components.append(_validate_component_authoring(manifest, project))
            for manifest in sorted(root.rglob("component.json")):
                if manifest.parent in authored_directories:
                    continue
                from literate_ai.compatibility.component_authoring import (
                    legacy_component_migration_message,
                )

                raise ProjectValidationError(
                    "project.component_migration_required",
                    legacy_component_migration_message(),
                )
        try:
            authority_graph = project_authority_graph(project.root)
        except AuthorityGraphError as exc:
            raise ProjectValidationError(exc.code, exc.message) from exc
        authority_review = _authority_review(
            project=project,
            repository_lineage=repository_lineage.identity,
            onboarding=onboarding,
            documentation=documentation,
            components=components,
            flavors=flavors,
            skills=skills,
            inverse_skills=inverse_skills,
            workflows=workflows,
            routing=routing,
            required=require_authority_review,
        )
        project_revision_identity = authority_review.authority_identity
        policy = project.definition.source_intelligence
        mode = policy.mode_for(source_intelligence_stage)
        source_intelligence_observation: dict[str, object] | None = None
        if mode is SourceIntelligenceMode.OFF or policy.provider_id == "none":
            source_intelligence_status = SourceIntelligenceStageStatus(
                source_intelligence_stage,
                SourceIntelligenceMode.OFF,
                "off",
                policy.provider_id,
            ).to_dict()
        elif policy.provider_id != "codegraph-cli":
            if mode is SourceIntelligenceMode.REQUIRED:
                raise ProjectValidationError(
                    "project.source_intelligence_provider_unsupported",
                    "required project source-intelligence provider is unsupported",
                )
            source_intelligence_status = SourceIntelligenceStageStatus(
                source_intelligence_stage,
                mode,
                "unavailable",
                policy.provider_id,
                "project.source_intelligence_provider_unsupported",
            ).to_dict()
        else:
            try:
                provider = CodeGraphProjectSourceIntelligence(policy)
                source_intelligence_observation = (
                    provider.sync(project.root)
                    if synchronize_source_intelligence
                    else provider.check(project.root)
                )
                source_intelligence_status = SourceIntelligenceStageStatus(
                    source_intelligence_stage, mode, "current", policy.provider_id
                ).to_dict()
            except ProjectSourceIntelligenceError as exc:
                if mode is SourceIntelligenceMode.REQUIRED:
                    raise ProjectValidationError(exc.code, exc.message) from exc
                source_intelligence_status = SourceIntelligenceStageStatus(
                    source_intelligence_stage,
                    mode,
                    "unavailable",
                    policy.provider_id,
                    exc.code,
                ).to_dict()
        configured_source_intelligence = layout["source_intelligence"]
        assert isinstance(configured_source_intelligence, dict)
        layout["source_intelligence"] = {
            **configured_source_intelligence,
            "status": source_intelligence_status,
            "observation": source_intelligence_observation,
        }
        result: dict[str, Any] = {
            "schema": PROJECT_VALIDATION_SCHEMA,
            **layout,
            "repository_parent": {
                "identity": parent_selection.identity.uri,
                "value": parent_selection.to_dict(),
            },
            "repository_lineage": {
                "identity": repository_lineage.identity.uri,
                "value": repository_lineage.to_dict(),
            },
            "authority_graph": authority_graph.to_dict(),
            "onboarding_skill": onboarding,
            "documentation": list(documentation["documents"]),
            "documentation_assets": list(documentation["assets"]),
            "documentation_graph": documentation["graph"],
            "components": components,
            "flavors": [
                {
                    "id": item.flavor_id,
                    "coordinate": item.coordinate_uri,
                    "axis": item.axis,
                    "value": item.value,
                    "revision_identity": item.revision_identity,
                    "specification_set_identity": item.specification_set_identity,
                }
                for item in flavors
            ],
            "workflows": list(workflows),
            "routing": list(routing),
            "specification_to_source_skills": [item.to_dict() for item in skills],
            "source_to_specification_skills": list(inverse_skills),
            "authority_review": authority_review.to_dict(),
            "monorepo_components": monorepo_components,
        }
        doc_advisories = _doc_identity_advisories(project)
        if doc_advisories:
            result["advisories"] = doc_advisories
        if include_test_receipt:
            try:
                result["test_receipt"] = inspect_project_test_receipt(
                    project, project_revision_identity=project_revision_identity
                )
            except ProjectError as exc:
                _error(exc, "project.test_receipt_invalid", "test receipt is invalid")
        return result

    def authority_identity(
        self, selected: Path, *, synchronize_source_intelligence: bool = True
    ) -> ContentIdentity:
        result = self.validate(
            selected,
            require_authority_review=True,
            include_test_receipt=False,
            synchronize_source_intelligence=synchronize_source_intelligence,
        )
        review = result["authority_review"]
        assert isinstance(review, dict)
        return ContentIdentity.parse_uri(str(review["authority_identity"]))

    def documentation_review(self, selected: Path) -> dict[str, object]:
        result = self.validate(
            selected,
            require_authority_review=False,
            synchronize_source_intelligence=False,
            source_intelligence_stage=SourceIntelligenceStage.STRUCTURAL_REVIEW,
            require_monorepo_current=False,
        )
        review = result["authority_review"]
        assert isinstance(review, dict)
        return review


def validate_project(
    selected: Path,
    *,
    require_authority_review: bool,
    include_test_receipt: bool = True,
    synchronize_source_intelligence: bool = True,
    source_intelligence_stage: SourceIntelligenceStage = (
        SourceIntelligenceStage.PROJECT_MAINTENANCE
    ),
) -> dict[str, Any]:
    return FilesystemProjectValidationAdapter().validate(
        selected,
        require_authority_review=require_authority_review,
        include_test_receipt=include_test_receipt,
        synchronize_source_intelligence=synchronize_source_intelligence,
        source_intelligence_stage=source_intelligence_stage,
    )


def validated_project_authority_identity(
    selected: Path, *, synchronize_source_intelligence: bool = True
) -> ContentIdentity:
    return FilesystemProjectValidationAdapter().authority_identity(
        selected,
        synchronize_source_intelligence=synchronize_source_intelligence,
    )


__all__ = [
    "FilesystemProjectValidationAdapter",
    "PROJECT_VALIDATION_SCHEMA",
    "ProjectValidationError",
    "validate_project",
    "validated_project_authority_identity",
]
