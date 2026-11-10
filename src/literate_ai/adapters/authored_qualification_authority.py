"""Reopen authored project and graph authority without execution or publication."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.authority import FileAuthorityProjectionStore
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.qualification_origins import AuthoredQualificationOrigin
from literate_ai.projects import (
    PROJECT_FILENAME,
    LoadedProject,
    PinnedInputClosure,
    ProjectConfigurationStore,
    discover_project,
)


class AuthoredQualificationAuthorityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CurrentAuthoredQualificationAuthority:
    """Still-live canonical inputs, not review or host-execution authorization.

    There is deliberately no deserializer from an origin reference. Planning,
    execution, publication and receiving must obtain this owner from current
    project discovery and lock admission, and still enforce their own independent
    verifier, review, permission and custody boundaries.
    """

    project: LoadedProject
    locked: LockedGenerationAuthoritySnapshot
    _origin: AuthoredQualificationOrigin = field(repr=False)
    _inputs: PinnedInputClosure = field(repr=False, compare=False)
    _component_projects: tuple[tuple[str, Path, Path], ...] = field(
        repr=False, compare=False
    )

    @property
    def origin(self) -> AuthoredQualificationOrigin:
        self.require_unchanged()
        return self._origin

    def require_unchanged(self) -> None:
        require_safe_directory(self.project.root)
        self._inputs.require_unchanged()
        self.locked.require_unchanged()
        _require_authored_components(self.project.root, self._component_projects)
        self._inputs.require_unchanged()
        self.locked.require_unchanged()


def _require_authored_components(
    project_root: Path, components: tuple[tuple[str, Path, Path], ...]
) -> None:
    roots = {project_root}
    for _coordinate, component_root, expected_project_root in components:
        current = discover_project(component_root)
        if current is None or current.root != expected_project_root:
            raise AuthoredQualificationAuthorityError(
                "qualification.authored.project_changed",
                "a selected Component's owning project changed during admission",
            )
        roots.add(current.root)
    selected = {coordinate for coordinate, _component, _project in components}
    for root in sorted(roots):
        # coordinates() distinguishes a missing store from corrupt/unsafe state.
        # Never catch a projection-read failure and reinterpret it as authored.
        projected = set(FileAuthorityProjectionStore(root).coordinates())
        if selected & projected:
            raise AuthoredQualificationAuthorityError(
                "qualification.authored.source_authority_present",
                "selected Components with source authority must use source-derived "
                "qualification and preserve its promotion obligations",
            )


def read_current_authored_qualification_authority(
    project_root: Path,
    request: GenerationPreparationRequest,
) -> CurrentAuthoredQualificationAuthority:
    """Read the exact current graph; do not allocate, generate or execute code.

    Root/configuration, all locked authorings and content, dependency membership,
    Flavors and resolution audit use their existing canonical readers. Every
    selected Component's owning project is reopened, including nested projects;
    a source-authority projection on any selected node refuses authored origin.
    """

    if not isinstance(request, GenerationPreparationRequest):
        raise TypeError("authored qualification requires a generation selection")
    root = Path(project_root).absolute()
    if ".." in root.parts:
        raise AuthoredQualificationAuthorityError(
            "qualification.authored.root_unsafe", "project root must be canonical"
        )
    require_safe_directory(root)
    configuration = ProjectConfigurationStore(root).read()
    component = request.component_root.absolute()
    require_safe_directory(component)
    if not component.is_relative_to(configuration.root):
        raise AuthoredQualificationAuthorityError(
            "qualification.authored.component_outside_project",
            "selected Component must be inside the selected project",
        )
    selected_project = discover_project(component)
    if selected_project is None or selected_project.root != configuration.root:
        raise AuthoredQualificationAuthorityError(
            "qualification.authored.project_mismatch",
            "selected root Component belongs to a different project",
        )
    locked = FilesystemLockedGenerationAuthorityReader().read(
        component,
        target_name=request.target_name,
        flavor_selectors=request.flavor_selectors,
        flavor_roots=request.flavor_roots,
    )
    inputs = locked.input_closure
    inputs.pin(
        configuration.root / PROJECT_FILENAME,
        boundary=configuration.root,
        label="authored-qualification-project",
        expected_content=configuration.content,
    )
    components = []
    for node in locked.authority.lock.nodes:
        node_root = locked.component_root(node.revision.authoring_identity)
        owner = discover_project(node_root)
        if owner is None:
            raise AuthoredQualificationAuthorityError(
                "qualification.authored.project_missing",
                "every selected Component requires current project discovery",
            )
        owner_configuration = ProjectConfigurationStore(owner.root).read()
        inputs.pin(
            owner.root / PROJECT_FILENAME,
            boundary=owner.root,
            label="authored-qualification-owner:" + node.revision.coordinate.uri,
            expected_content=owner_configuration.content,
        )
        components.append((node.revision.coordinate.uri, node_root, owner.root))
    origin = AuthoredQualificationOrigin(
        configuration.content_identity,
        locked.authority.identity,
        locked.authority.lock.identity,
        ContentIdentity.parse_uri(inputs.identity),
    )
    result = CurrentAuthoredQualificationAuthority(
        LoadedProject(configuration.root, configuration.definition),
        locked,
        origin,
        inputs,
        tuple(components),
    )
    result.require_unchanged()
    return result
